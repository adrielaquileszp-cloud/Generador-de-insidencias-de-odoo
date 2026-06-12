"""
incidencias_odoo.py
====================
Módulo de apoyo (NO es una app — lo importa streamlit_app.py).

Convierte las incongruencias del cruce de inventario en TAREAS de la app
Proyecto de Odoo (compatible con Odoo SaaS, sin módulos custom, todo vía
XML-RPC estándar).

LÓGICA: UNA TAREA POR FOLIO.
Todas las incongruencias de un mismo folio S se aglomeran en una sola
tarea, con una tabla de productos en la descripción.

Ciclo que implementa:
  1. Asegura que exista el proyecto "Incidencias de Inventario" y sus etapas.
  2. Calcula el MONTO de cada producto (diferencia x precio de venta) y el
     MONTO TOTAL del folio; la prioridad del folio se decide por dinero.
  3. Crea una tarea por folio, asignada al supervisor de la tienda
     (Odoo le manda correo automáticamente al asignar; OJO: en bases de
     prueba duplicadas, Odoo desactiva el envío de correos).
  4. ANTI-DUPLICADOS: usa una clave [folio] en el nombre de la tarea.
     Si la tarea del folio ya existe abierta, se actualiza (la tabla de
     productos se refresca con el cruce más reciente).
  5. AUTO-CIERRE: si un folio se re-verificó en el cruce (parámetro
     folios_revisados) y ya no trae ninguna incongruencia, su tarea se
     mueve a "Resuelta" con un comentario automático.
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

# Prioridad por DINERO: monto total del folio
# (suma de diferencia x precio de venta de todos sus productos).
# monto_total >= UMBRAL_PRIORIDAD_ALTA -> estrella + etiqueta "$$$ Prioridad alta"
# Ajustar este umbral con gerencia.
UMBRAL_PRIORIDAD_ALTA = 500.0   # pesos

# Días para resolver (fecha límite de la tarea)
DIAS_LIMITE = 3

# Usuario responsable del lado CEDIS / Bio Zen (res.users id).
# Toda incidencia se asigna TANTO al usuario de la tienda COMO a este
# usuario, para que ambos lados revisen (el error puede ser de cualquiera).
CEDIS_USER_ID = 46   # Coordinadora de Tiendas

# Mapeo SUCURSAL -> user_id en Odoo (res.users).
# Ids extraídos del export de usuarios (patrón __export__.res_users_<id>_...).
# Las sucursales sin usuario mapeado quedan sin asignar, en etapa "Nueva".
SUPERVISORES = {
    "CENTRO": 17,
    "CONCHI": 18,
    "CARRASCO": 19,
    "SERDAN": 20,
    "ROSARIO": 21,
    "ESCUINAPA": 22,
    "INSURGENTES": 23,
    "VILLA UNIÓN": 24,
    "SANTA ROSA": 25,
    "LEY VIEJA": 26,
    "CARIBE": 27,
    "LEY DEL MAR": 28,
    "BRAVO": 29,
    "RELIGIOSO": 31,        # S-18 RELIGIOSO CENTRO (confirmado)
    "VILLA VERDE": 34,
    "ESCOBEDO": 35,
    "FORJADORES": 33,
    "GUAYMITAS": 36,
    "MERCADITO": 37,
    "LOS MANGOS": 38,
    "COLA DE BALLENA": 39,
    "SAN JOSÉ VIEJO": 40,
    "TOREO": 41,
    "HIDALGO": 42,
    "COLOSIO": 81,
    "LA CAMPIÑA": 86,
    "SUPER GPNA": 99,
    "AQUILES": 20,          # la atiende el usuario de SERDAN (S-05)
    "MELCHOR OCAMPO": 78,   # la atiende el usuario de MEXICA (S-33)
    # Sin usuario — confirmar o dejar sin asignar:
    # "DELIVERY": ?,
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
# 3) UNA TAREA POR FOLIO
# ============================================================

def _clave(folio):
    """Clave única anti-duplicados (por folio). Va al inicio del nombre."""
    return f"[{folio}]"


def _descripcion_folio(folio, grupo, precios, monto_total):
    """Arma la descripción HTML con la tabla de productos del folio."""
    primera = grupo.iloc[0]

    filas = ""
    for _, r in grupo.iterrows():
        pid = int(r['ProductoID'])
        precio = precios.get(pid, 0.0)
        monto = abs(r['Diferencia']) * precio
        color = "#b91c1c" if r['Estado'] == 'FALTANTE' else "#b45309"
        filas += (
            f"<tr>"
            f"<td style='padding:3px 8px;'>{r['Producto']}</td>"
            f"<td style='padding:3px 8px; text-align:center;'>{r['UdM']}</td>"
            f"<td style='padding:3px 8px; text-align:right;'>{r['Surtido']:.0f}</td>"
            f"<td style='padding:3px 8px; text-align:right;'>{r['Recibido']:.0f}</td>"
            f"<td style='padding:3px 8px; text-align:right;'><b>{r['Diferencia']:.0f}</b></td>"
            f"<td style='padding:3px 8px; color:{color};'><b>{r['Estado']}</b></td>"
            f"<td style='padding:3px 8px; text-align:right;'>${precio:,.2f}</td>"
            f"<td style='padding:3px 8px; text-align:right;'><b>${monto:,.2f}</b></td>"
            f"</tr>"
        )

    n_falt = int((grupo['Estado'] == 'FALTANTE').sum())
    n_sobr = int((grupo['Estado'] == 'SOBRANTE').sum())

    return (
        f"<p><b>Cruce de inventario CEDIS → Tiendas</b></p>"
        f"<ul>"
        f"<li>Folio venta: {folio} | Folio compra: {primera.get('Folio Compra', '-')}</li>"
        f"<li>Sucursal: {primera['Sucursal']}</li>"
        f"<li>Productos con diferencia: {len(grupo)} "
        f"({n_falt} faltantes, {n_sobr} sobrantes)</li>"
        f"<li><b>Monto total en riesgo: ${monto_total:,.2f}</b></li>"
        f"<li>Docs surtido: {primera.get('Docs Surtido', '-')}</li>"
        f"<li>Docs recepción: {primera.get('Docs Recepción', '-')}</li>"
        f"</ul>"
        f"<table style='border-collapse:collapse; font-size:13px;' border='1'>"
        f"<tr style='background:#f3f4f6;'>"
        f"<th style='padding:4px 8px;'>Producto</th>"
        f"<th style='padding:4px 8px;'>UdM</th>"
        f"<th style='padding:4px 8px;'>Surtido</th>"
        f"<th style='padding:4px 8px;'>Recibido</th>"
        f"<th style='padding:4px 8px;'>Dif.</th>"
        f"<th style='padding:4px 8px;'>Tipo</th>"
        f"<th style='padding:4px 8px;'>Precio venta</th>"
        f"<th style='padding:4px 8px;'>Monto</th>"
        f"</tr>"
        f"{filas}"
        f"</table>"
        f"<p>Detectado el {datetime.now().strftime('%d/%m/%Y %H:%M')}. "
        f"Por favor revisa el folio completo, corrige y mueve la tarea a la "
        f"etapa que corresponda indicando el motivo en un comentario.</p>"
    )


def generar_incidencias(models, db, uid, pwd, df_incongruencias,
                        folios_revisados=None, progreso=None):
    """
    df_incongruencias: SOLO filas con Estado FALTANTE o SOBRANTE, con columnas:
        Folio Venta, Folio Compra, Sucursal, Producto, ProductoID, UdM,
        Surtido, Recibido, Diferencia, Estado, Docs Surtido, Docs Recepción

    folios_revisados: set con TODOS los folios verificados en el cruce
        (incluyendo los que salieron OK). Es lo que permite el AUTO-CIERRE:
        una tarea abierta se cierra solo si su folio fue re-verificado y ya
        no aparece con diferencias. Si no se pasa, no se cierra nada.

    Crea/actualiza UNA tarea por folio, aglomerando todos sus productos.
    Recibe la conexión ya hecha (models, db, uid, pwd) — no maneja credenciales.
    Devuelve dict: {'creadas', 'actualizadas', 'cerradas', 'sin_supervisor'}
    (los contadores son por FOLIO, no por producto).
    """
    project_id, etapas = asegurar_proyecto(models, db, uid, pwd)
    tags = asegurar_tags(models, db, uid, pwd)

    # --- precios ---
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

    grupos = list(df_incongruencias.groupby('Folio Venta'))
    total = len(grupos)

    for n, (folio, grupo) in enumerate(grupos):
        if progreso:
            progreso(n + 1, total)

        clave = _clave(folio)
        claves_actuales.add(clave)

        # Monto total del folio (suma de diferencia x precio por producto)
        monto_total = 0.0
        for _, r in grupo.iterrows():
            monto_total += abs(r['Diferencia']) * precios.get(int(r['ProductoID']), 0.0)
        monto_total = round(monto_total, 2)
        es_alta = monto_total >= UMBRAL_PRIORIDAD_ALTA

        # Etiquetas: puede llevar ambas si el folio tiene faltantes y sobrantes
        tag_ids = []
        if (grupo['Estado'] == 'FALTANTE').any():
            tag_ids.append(tags[TAG_FALTANTE])
        if (grupo['Estado'] == 'SOBRANTE').any():
            tag_ids.append(tags[TAG_SOBRANTE])
        if es_alta:
            tag_ids.append(tags[TAG_ALTA])

        sucursal = grupo.iloc[0]['Sucursal']
        user_tienda = SUPERVISORES.get(sucursal)
        if not user_tienda:
            stats['sin_supervisor'].add(sucursal)

        # Asignación a AMBOS lados: tienda + CEDIS (el error puede ser de cualquiera)
        asignados = [u for u in (user_tienda, CEDIS_USER_ID) if u]

        n_prod = len(grupo)
        nombre = (f"{clave} {sucursal} · {n_prod} producto{'s' if n_prod != 1 else ''} "
                  f"con diferencia · ${monto_total:,.0f}")

        vals = {
            'name': nombre,
            'project_id': project_id,
            'description': _descripcion_folio(folio, grupo, precios, monto_total),
            'priority': '1' if es_alta else '0',
            'tag_ids': [(6, 0, tag_ids)],
            'date_deadline': (datetime.now() + timedelta(days=DIAS_LIMITE)).strftime('%Y-%m-%d'),
        }

        if clave in abiertas_por_clave:
            # La tarea del folio ya existe y sigue abierta -> refrescar (no duplicar)
            _kw(models, db, uid, pwd, 'project.task', 'write',
                [[abiertas_por_clave[clave]], {
                    'name': vals['name'],
                    'description': vals['description'],
                    'priority': vals['priority'],
                    'tag_ids': vals['tag_ids'],
                }])
            stats['actualizadas'] += 1
        else:
            vals['stage_id'] = etapas['Asignada' if asignados else 'Nueva']
            if asignados:
                vals['user_ids'] = [(6, 0, asignados)]  # dispara correo a cada asignado
            _kw(models, db, uid, pwd, 'project.task', 'create', [vals])
            stats['creadas'] += 1

    # --- AUTO-CIERRE: folios re-verificados que ya no traen ninguna diferencia ---
    if folios_revisados:
        folios_revisados = set(folios_revisados)
        for clave, task_id in abiertas_por_clave.items():
            if clave in claves_actuales:
                continue
            folio = clave[1:-1]  # quita los corchetes [ ]
            if folio not in folios_revisados:
                # Este folio no se revisó en este cruce: no tocar.
                continue
            _kw(models, db, uid, pwd, 'project.task', 'write',
                [[task_id], {'stage_id': etapas['Resuelta']}])
            _kw(models, db, uid, pwd, 'project.task', 'message_post', [[task_id]], {
                'body': "Cerrada automáticamente: en el cruce más reciente este "
                        "folio ya no presenta ninguna diferencia.",
            })
            stats['cerradas'] += 1

    stats['sin_supervisor'] = sorted(stats['sin_supervisor'])
    return stats
