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
SUPABASE_JWT  = os.environ['SUPABASE_SERVICE_KEY']

BOOKINGS_DB   = 'eb2f8685-7f3f-824c-9312-873814b1d7d6'

def notion_req(path, payload=None):
    url = f'https://api.notion.com/v1{path}'
    data = json.dumps(payload).encode() if payload else None
    method = 'POST' if payload else 'GET'
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f'[NOTION ERROR] {e.code} {path}: {body[:400]}', flush=True)
        raise

def supabase_req(path, payload=None, method='POST'):
    url = f'{SUPABASE_URL}/rest/v1{path}'
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_JWT}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    })
    try:
        with urllib.request.urlopen(req) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f'[SUPABASE ERROR] {e.code} {path}: {body[:400]}', flush=True)
        raise

def get_prop(page, name):
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
        return [r['id'] for r in p.get('relation', [])]
    if t == 'rollup':
        ro = p.get('rollup', {})
        if ro.get('type') == 'number': return ro.get('number')
        if ro.get('type') == 'array':
            for item in ro.get('array', []):
                if item.get('type') == 'number': return item.get('number')
    return None

tariff_cache  = {}
airport_cache = {}

def get_tariff(tid):
    if tid in tariff_cache: return tariff_cache[tid]
    try:
        page = notion_req(f'/pages/{tid}')
        tariff_cache[tid] = {
            'compra_kg': get_prop(page, 'Compra (USD/kg)') or 0,
            'venda_kg':  get_prop(page, 'Venda (USD/kg)')  or 0,
        }
    except:
        tariff_cache[tid] = {'compra_kg': 0, 'venda_kg': 0}
    return tariff_cache[tid]

def get_airport(pid):
    if pid in airport_cache: return airport_cache[pid]
    try:
        page = notion_req(f'/pages/{pid}')
        name = None
        for prop in page.get('properties', {}).values():
            if prop.get('type') == 'title':
                name = ''.join(x['plain_text'] for x in prop.get('title', []))
                break
        airport_cache[pid] = name or pid[:6]
    except:
        airport_cache[pid] = pid[:6]
    return airport_cache[pid]

def fetch_bookings():
    bookings = []
    cursor = None
    page_num = 0

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

        page_num += 1
        print(f'  Buscando página {page_num}...', flush=True)
        resp = notion_req(f'/databases/{BOOKINGS_DB}/query', payload)
        results = resp.get('results', [])
        print(f'  → {len(results)} registros', flush=True)

        for page in results:
            origem_ids  = get_prop(page, 'ORIGEM')  or []
            destino_ids = get_prop(page, 'DESTINO') or []
            tarifa_ids  = get_prop(page, 'Tarifa')  or []

            origem  = get_airport(origem_ids[0])  if origem_ids  else None
            destino = get_airport(destino_ids[0]) if destino_ids else None
            tarifa  = get_tariff(tarifa_ids[0])   if tarifa_ids  else {'compra_kg':0,'venda_kg':0}

            bookings.append({
                'mawb':      get_prop(page, 'MAWB') or '',
                'data':      get_prop(page, 'DATA'),
                'origem':    origem,
                'destino':   destino,
                'peso':      get_prop(page, 'Peso reservado (kg)') or 0,
                'skids':     get_prop(page, 'Skids/caixas reservados') or 0,
                'compra_kg': tarifa['compra_kg'],
                'venda_kg':  tarifa['venda_kg'],
            })

        if not resp.get('has_more'):
            break
        cursor = resp.get('next_cursor')

    return bookings

def main():
    print(f'[{datetime.now(timezone.utc).isoformat()}] Iniciando sync KPI...', flush=True)

    # Testar conexão Notion primeiro
    print('Testando conexão com o Notion...', flush=True)
    try:
        db_info = notion_req(f'/databases/{BOOKINGS_DB}')
        title = db_info.get('title', [{}])
        db_name = title[0].get('plain_text', '?') if title else '?'
        print(f'✓ Notion conectado — base: {db_name}', flush=True)
    except Exception as e:
        print(f'✗ Falha ao conectar ao Notion: {e}', flush=True)
        print('Verifique se o NOTION_TOKEN está correto e se a integração foi conectada à base Bookings.', flush=True)
        sys.exit(1)

    print('Buscando bookings confirmados...', flush=True)
    bookings = fetch_bookings()
    print(f'Total: {len(bookings)} bookings', flush=True)

    if not bookings:
        print('Nenhum booking — abortando sem salvar', flush=True)
        sys.exit(0)

    # Salvar no Supabase
    print('Salvando snapshot no Supabase...', flush=True)
    supabase_req('/kpi_snapshots', {'data': bookings})
    print(f'✓ Snapshot salvo — {len(bookings)} embarques', flush=True)
    print(f'[{datetime.now(timezone.utc).isoformat()}] Sync concluído ✓', flush=True)

if __name__ == '__main__':
    main()
