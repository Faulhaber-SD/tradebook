#!/usr/bin/env python3
"""
TradeBook KPI Sync
Busca bookings confirmados do Notion e salva snapshot no Supabase.
Roda via GitHub Actions todo dia às 07:00 BRT (10:00 UTC).
"""

import os, json, sys, urllib.request, urllib.error
from datetime import datetime, timezone

NOTION_TOKEN  = os.environ['NOTION_TOKEN']
SUPABASE_URL  = 'https://damjldmgwksxigxtkass.supabase.co'
SUPABASE_KEY  = os.environ['SUPABASE_ANON_KEY']
SUPABASE_JWT  = os.environ['SUPABASE_SERVICE_KEY']  # service role para bypass RLS

BOOKINGS_DB   = 'eb2f8685-7f3f-824c-9312-873814b1d7d6'
TARIFF_DB     = '6fff8685-7f3f-828c-8e14-8745496ebfe3'

# ── helpers ──────────────────────────────────────────────

def notion_req(path, payload=None):
    url = f'https://api.notion.com/v1{path}'
    data = json.dumps(payload).encode() if payload else None
    method = 'POST' if payload else 'GET'
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def supabase_req(path, payload=None, method='POST'):
    url = f'{SUPABASE_URL}/rest/v1{path}'
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_JWT}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    })
    with urllib.request.urlopen(req) as r:
        return r.read()

def get_prop(page, name, kind='text'):
    props = page.get('properties', {})
    p = props.get(name)
    if not p: return None
    t = p.get('type')
    if t == 'title':
        return ''.join(x['plain_text'] for x in p.get('title', []))
    if t == 'date':
        d = p.get('date')
        return d['start'] if d else None
    if t == 'number':
        return p.get('number')
    if t == 'status':
        s = p.get('status')
        return s['name'] if s else None
    if t == 'relation':
        rels = p.get('relation', [])
        return [r['id'] for r in rels]
    if t == 'rollup':
        ro = p.get('rollup', {})
        if ro.get('type') == 'number': return ro.get('number')
        if ro.get('type') == 'array':
            for item in ro.get('array', []):
                if item.get('type') == 'number': return item.get('number')
    return None

# ── buscar tarifa (cache por ID) ──────────────────────────

tariff_cache = {}

def get_tariff(tariff_id):
    if tariff_id in tariff_cache:
        return tariff_cache[tariff_id]
    try:
        page = notion_req(f'/pages/{tariff_id}')
        compra = get_prop(page, 'Compra (USD/kg)', 'number')
        venda  = get_prop(page, 'Venda (USD/kg)',  'number')
        tariff_cache[tariff_id] = {'compra_kg': compra or 0, 'venda_kg': venda or 0}
    except:
        tariff_cache[tariff_id] = {'compra_kg': 0, 'venda_kg': 0}
    return tariff_cache[tariff_id]

# ── buscar nome do aeroporto (cache) ─────────────────────

airport_cache = {}

def get_airport(page_id):
    if page_id in airport_cache:
        return airport_cache[page_id]
    try:
        page = notion_req(f'/pages/{page_id}')
        name = get_prop(page, 'Nome') or get_prop(page, 'Name') or get_prop(page, 'Aeroporto')
        # tentar title genérico
        if not name:
            for prop in page.get('properties', {}).values():
                if prop.get('type') == 'title':
                    name = ''.join(x['plain_text'] for x in prop.get('title', []))
                    break
        airport_cache[page_id] = name or page_id[:8]
    except:
        airport_cache[page_id] = page_id[:8]
    return airport_cache[page_id]

# ── paginar bookings confirmados ──────────────────────────

def fetch_bookings():
    bookings = []
    cursor = None
    page_count = 0

    while True:
        payload = {
            'filter': {
                'and': [
                    {'property': 'Status da reserva', 'status': {'equals': 'Confirmado'}},
                    {'property': 'DATA', 'date': {'is_not_empty': True}},
                ]
            },
            'sorts': [{'property': 'DATA', 'direction': 'descending'}],
            'page_size': 100,
        }
        if cursor:
            payload['start_cursor'] = cursor

        resp = notion_req(f'/databases/{BOOKINGS_DB}/query', payload)
        results = resp.get('results', [])
        page_count += 1
        print(f'  Página {page_count}: {len(results)} registros', flush=True)

        for page in results:
            mawb   = get_prop(page, 'MAWB')
            data   = get_prop(page, 'DATA')
            peso   = get_prop(page, 'Peso reservado (kg)')
            skids  = get_prop(page, 'Skids/caixas reservados')

            # Origem e destino (relação → nome do aeroporto)
            origem_ids  = get_prop(page, 'ORIGEM',  'relation') or []
            destino_ids = get_prop(page, 'DESTINO', 'relation') or []
            origem  = get_airport(origem_ids[0])  if origem_ids  else None
            destino = get_airport(destino_ids[0]) if destino_ids else None

            # Tarifa vinculada
            tarifa_ids = get_prop(page, 'Tarifa', 'relation') or []
            tarifa = get_tariff(tarifa_ids[0]) if tarifa_ids else {'compra_kg':0,'venda_kg':0}

            bookings.append({
                'mawb':      mawb or '',
                'data':      data,
                'origem':    origem,
                'destino':   destino,
                'peso':      peso or 0,
                'skids':     skids or 0,
                'compra_kg': tarifa['compra_kg'],
                'venda_kg':  tarifa['venda_kg'],
            })

        if not resp.get('has_more'):
            break
        cursor = resp.get('next_cursor')

    return bookings

# ── main ─────────────────────────────────────────────────

def main():
    print(f'[{datetime.now(timezone.utc).isoformat()}] Iniciando sync KPI...', flush=True)

    print('Buscando bookings confirmados do Notion...', flush=True)
    bookings = fetch_bookings()
    print(f'Total: {len(bookings)} bookings confirmados', flush=True)

    if not bookings:
        print('Nenhum booking encontrado — abortando sem salvar', flush=True)
        sys.exit(0)

    # Salvar snapshot no Supabase
    print('Salvando snapshot no Supabase...', flush=True)
    supabase_req('/kpi_snapshots', {'data': bookings})
    print('Snapshot salvo com sucesso!', flush=True)

    # Manter só últimos 30
    print('Limpando snapshots antigos...', flush=True)
    # O trigger no banco já faz isso automaticamente

    print(f'[{datetime.now(timezone.utc).isoformat()}] Sync concluído ✓', flush=True)

if __name__ == '__main__':
    main()
