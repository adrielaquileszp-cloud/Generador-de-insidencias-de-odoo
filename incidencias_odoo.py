"""
incidencias_odoo.py
====================
Módulo de apoyo (NO es una app — lo importa streamlit_app.py).

Convierte las incongruencias del cruce de inventario en TAREAS de la app
Proyecto de Odoo (compatible con Odoo SaaS, sin módulos custom, todo vía
XML-RPC estándar).

Ciclo que implementa:
  1. Asegura que exista el proyecto "Incidencias de Inventario" y sus etapas.
  2. Calcula el MONTO de cada diferencia (diferencia x precio de venta)
     y asigna prioridad por dinero.
  3. Crea una tarea por incongruencia, asignada al supervisor de la tienda
     (Odoo le manda correo automáticamente al asignar; OJO: en bases de
     prueba duplicadas, Odoo desactiva el envío de correos).
  4. ANTI-DUPLICADOS: usa una clave [folio|producto] en el nombre de la
     tarea. Si ya existe abierta, actualiza en lugar de duplicar.
  5. AUTO-CIERRE: si en un cruce posterior la diferencia ya no aparece
     (y el folio sí se re-verificó), mueve la tarea a "Resuelta" con un
     comentario automático.
"""

from datetime import datetime, timedelta

# ============================================================
# CONFIGURACIÓN
# ============================================================

NOMBRE_PROYECTO = "Incidencias de Inventario CEDIS-GPNA"

# Etapas del ciclo (columnas del kanban), en orden.
# fold=True las colapsa en el kanban (para las cerradas).
ETAPAS = [
    ("Nueva", False),
    ("Asignada", False),
    ("En revisión", False),
    ("Resuelta", True),
    ("Justificada", True),
    ("No procede", True),
]
ETAPAS_CERRADAS = {"Resuelta", "Justificada", "No procede"}

# Prioridad por DINERO (precio de venta x piezas de diferencia).
# monto >= UMBRAL_PRIORIDAD_ALTA -> estrella + etiqueta "$$$ Prioridad alta"
# Ajustar este umbral con gerencia.
UMBRAL_PRIORIDAD_ALTA = 500.0   # pesos

# Días para resolver (fecha límite de la tarea)
DIAS_LIMITE = 3

# Mapeo SUCURSAL -> user_id del supervisor en Odoo (res.users).
# *** LLENAR con los ids reales. ***
# Las sucursales sin supervisor quedan sin asignar, en etapa "Nueva".
SUPERVISORES = {
    # "CENTRO": 12,
}

# Etiquetas que se crean/reusan automáticamente
TAG_FALTANTE = "Faltante"
TAG_SOBRANTE = "Sobrante"
TAG_ALTA = "$$$ Prioridad alta"


# ============================================================
# HELPERS XML-RPC
# ============================================================

def _kw(models, db, uid, pwd, model, method, args, kwargs=None):
    return models.execute_kw(db, uid, pwd, model, method, args, kwargs or {})


def _buscar_o_crear(models, db, uid, pwd, model, domain, vals):
    ids = _kw(models, db, uid, pwd, model, 'search', [domain], {'limit': 1})
    if ids:
        return ids[0]
    return _kw(models, db, uid, pwd, model, 'create', [vals])


# ============================================================
# 1) PROYECTO Y ETAPAS
# ============================================================

def asegurar_proyecto(models, db, uid, pwd):
    """Devuelve (project_id, {nombre_etapa: stage_id}). Crea lo que falte."""
    project_id = _buscar_o_crear(
        models, db, uid, pwd, 'project.project',
        [('name', '=', NOMBRE_PROYECTO)],
        {'name': NOMBRE_PROYECTO},
    )

    etapas = {}
    for i, (nombre, fold) in enumerate(ETAPAS):
        stage_ids = _kw(models, db, uid, pwd, 'project.task.type', 'search',
                        [[('name', '=', nombre), ('project_ids', 'in', [project_id])]],
                        {'limit': 1})
        if stage_ids:
            etapas[nombre] = stage_ids[0]
        else:
            etapas[nombre] = _kw(models, db, uid, pwd, 'project.task.type', 'create', [{
                'name': nombre,
                'sequence': i,
                'fold': fold,
                'project_ids': [(4, project_id)],
            }])
    return project_id, etapas


def asegurar_tags(models, db, uid, pwd):
    tags = {}
    for nombre in (TAG_FALTANTE, TAG_SOBRANTE, TAG_ALTA):
        tags[nombre] = _buscar_o_crear(
            models, db, uid, pwd, 'project.tags',
            [('name', '=', nombre)], {'name': nombre},
        )
    return tags


# ============================================================
# 2) PRECIOS DE VENTA
# ============================================================

def obtener_precios(models, db, uid, pwd, product_ids):
    """Devuelve {product_id: list_price} para los productos dados."""
    if not product_ids:
        return {}
    datos = _kw(models, db, uid, pwd, 'product.product', 'read',
                [list(set(int(p) for p in product_ids))], {'fields': ['list_price']})
    return {d['id']: d.get('list_price') or 0.0 for d in datos}


# ============================================================
# 3) CREAR / ACTUALIZAR / CERRAR INCIDENCIAS
# ============================================================

def _clave(folio, product_id):
    """Clave única anti-duplicados. Va al inicio del nombre de la tarea."""
    return f"[{folio}|{int(product_id)}]"


def _descripcion(row, precio, monto):
    return (
        f"<p><b>Cruce de inventario CEDIS → Tiendas</b></p>"
        f"<ul>"
        f"<li>Folio venta: {row['Folio Venta']} | Folio compra: {row.get('Folio Compra', '-')}</li>"
        f"<li>Sucursal: {row['Sucursal']}</li>"
        f"<li>Producto: {row['Producto']} ({row['UdM']})</li>"
        f"<li>Surtido: {row['Surtido']:.0f} | Recibido: {row['Recibido']:.0f} | "
        f"<b>Diferencia: {row['Diferencia']:.0f} ({row['Estado']})</b></li>"
        f"<li>Precio de venta: ${precio:,.2f} | <b>Monto en riesgo: ${monto:,.2f}</b></li>"
        f"<li>Docs surtido: {row.get('Docs Surtido', '-')}</li>"
        f"<li>Docs recepción: {row.get('Docs Recepción', '-')}</li>"
        f"</ul>"
        f"<p>Detectado el {datetime.now().strftime('%d/%m/%Y %H:%M')}. "
        f"Por favor revisa, corrige y mueve la tarea a la etapa que corresponda "
        f"indicando el motivo en un comentario.</p>"
    )


def generar_incidencias(models, db, uid, pwd, df_incongruencias, progreso=None):
    """
    df_incongruencias: SOLO filas con Estado FALTANTE o SOBRANTE, con columnas:
        Folio Venta, Folio Compra, Sucursal, Producto, ProductoID, UdM,
        Surtido, Recibido, Diferencia, Estado, Docs Surtido, Docs Recepción

    Recibe la conexión ya hecha (models, db, uid, pwd) — no maneja credenciales.
    Devuelve dict: {'creadas', 'actualizadas', 'cerradas', 'sin_supervisor'}.
    """
    project_id, etapas = asegurar_proyecto(models, db, uid, pwd)
    tags = asegurar_tags(models, db, uid, pwd)

    # --- precios y montos ---
    precios = obtener_precios(models, db, uid, pwd, df_incongruencias['ProductoID'].tolist())

    # --- tareas abiertas existentes del proyecto (para dedupe y auto-cierre) ---
    etapa_ids_cerradas = [etapas[n] for n in ETAPAS_CERRADAS]
    abiertas = _kw(models, db, uid, pwd, 'project.task', 'search_read',
                   [[('project_id', '=', project_id),
                     ('stage_id', 'not in', etapa_ids_cerradas)]],
                   {'fields': ['id', 'name']})
    abiertas_por_clave = {}
    for t in abiertas:
        if t['name'].startswith('['):
            clave = t['name'].split(']')[0] + ']'
            abiertas_por_clave[clave] = t['id']

    stats = {'creadas': 0, 'actualizadas': 0, 'cerradas': 0, 'sin_supervisor': set()}
    claves_actuales = set()
    folios_en_cruce = set(df_incongruencias['Folio Venta'])

    total = len(df_incongruencias)
    for n, (_, row) in enumerate(df_incongruencias.iterrows()):
        if progreso:
            progreso(n + 1, total)

        pid = int(row['ProductoID'])
        clave = _clave(row['Folio Venta'], pid)
        claves_actuales.add(clave)

        precio = precios.get(pid, 0.0)
        monto = round(abs(row['Diferencia']) * precio, 2)
        es_alta = monto >= UMBRAL_PRIORIDAD_ALTA

        tag_ids = [tags[TAG_FALTANTE if row['Estado'] == 'FALTANTE' else TAG_SOBRANTE]]
        if es_alta:
            tag_ids.append(tags[TAG_ALTA])

        user_id = SUPERVISORES.get(row['Sucursal'])
        if not user_id:
            stats['sin_supervisor'].add(row['Sucursal'])

        vals = {
            'name': f"{clave} {row['Estado']} · {row['Sucursal']} · {row['Producto'][:60]}",
            'project_id': project_id,
            'description': _descripcion(row, precio, monto),
            'priority': '1' if es_alta else '0',
            'tag_ids': [(6, 0, tag_ids)],
            'date_deadline': (datetime.now() + timedelta(days=DIAS_LIMITE)).strftime('%Y-%m-%d'),
        }

        if clave in abiertas_por_clave:
            # Ya existe y sigue abierta -> actualizar datos (no duplicar)
            _kw(models, db, uid, pwd, 'project.task', 'write',
                [[abiertas_por_clave[clave]], {
                    'description': vals['description'],
                    'priority': vals['priority'],
                    'tag_ids': vals['tag_ids'],
                }])
            stats['actualizadas'] += 1
        else:
            vals['stage_id'] = etapas['Asignada' if user_id else 'Nueva']
            if user_id:
                vals['user_ids'] = [(6, 0, [user_id])]  # dispara correo automático
            _kw(models, db, uid, pwd, 'project.task', 'create', [vals])
            stats['creadas'] += 1

    # --- AUTO-CIERRE: abiertas cuyo folio se re-verificó y ya no traen diferencia ---
    for clave, task_id in abiertas_por_clave.items():
        if clave in claves_actuales:
            continue
        folio = clave[1:].split('|')[0]
        if folio not in folios_en_cruce:
            # Este folio no se revisó en este cruce: no tocar.
            continue
        _kw(models, db, uid, pwd, 'project.task', 'write',
            [[task_id], {'stage_id': etapas['Resuelta']}])
        _kw(models, db, uid, pwd, 'project.task', 'message_post', [[task_id]], {
            'body': "Cerrada automáticamente: en el cruce más reciente esta "
                    "diferencia ya no aparece (la cantidad surtida y recibida coinciden).",
        })
        stats['cerradas'] += 1

    stats['sin_supervisor'] = sorted(stats['sin_supervisor'])
    return stats
