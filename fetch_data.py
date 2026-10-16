"""
Henter kartdata fra Sodir (Norwegian Offshore Directorate) FactMaps og lagrer
dem som kompakte GeoJSON-filer under data/ for ppt.html.

Kjøres automatisk daglig av .github/workflows/update-map-data.yml, men kan
også kjøres lokalt (kun standardbiblioteket, ingen pip install):

    python fetch_data.py

Utdata er deterministisk (sorterte features og nøkler), slik at filene bare
endrer seg når selve dataene har endret seg. data/meta.json oppdateres bare
når noe annet har endret seg, så workflowen ikke lager en commit hver dag.

Bakgrunnskartet (data/basemap.json: kystlinje, landegrenser, dybdekurver) er
statisk og bygget én gang fra Natural Earth 10m med mapshaper - se README.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = 'https://factmaps.sodir.no/api/rest/services/DataService/Data/MapServer'
OUT_DIR = 'data'
PAGE_SIZE = 2000
TIMEOUT = 180
RETRIES = 4
# Geometriforenkling i grader (ca. 50 m nord-sør) og antall desimaler (~10 m).
SIMPLIFY = 0.0005
PRECISION = 4

SOURCE = 'Sokkeldirektoratet (Sodir) FactMaps - https://factmaps.sodir.no - NLOD 2.0'


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _get(url, params):
    """GET med retry og eksponentiell backoff. Sodir svarer av og til tregt."""
    full = f'{url}?{urllib.parse.urlencode(params)}'
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(full, headers={'User-Agent': 'ppt-map-fetch'})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            if 'error' in data:
                raise RuntimeError(data['error'].get('message', 'ukjent API-feil'))
            return data
        except Exception as exc:  # noqa: BLE001 - vi vil retry-e på alt
            last = exc
            if attempt < RETRIES - 1:
                wait = 2 ** (attempt + 1)
                print(f'    ! {exc} - prøver igjen om {wait}s')
                time.sleep(wait)
    raise RuntimeError(f'Ga opp etter {RETRIES} forsøk: {last}')


def fetch(layer, out_fields, where='1=1', geometry=True):
    """Henter alle rader fra et lag, paginert og sortert på OBJECTID."""
    rows, offset = [], 0
    while True:
        params = {
            'where': where,
            'outFields': ','.join(out_fields),
            'orderByFields': 'OBJECTID',
            'resultOffset': offset,
            'resultRecordCount': PAGE_SIZE,
            'returnGeometry': 'true' if geometry else 'false',
            'f': 'geojson' if geometry else 'json',
        }
        if geometry:
            params.update(outSR=4326, maxAllowableOffset=SIMPLIFY, geometryPrecision=PRECISION)
        data = _get(f'{BASE}/{layer}/query', params)
        page = data.get('features', [])
        if not geometry:
            page = [{'properties': f['attributes']} for f in page]
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    print(f'  lag {layer}: {len(rows)} rader')
    return rows


def num(value):
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


# Forkortelser som skal beholde store bokstaver i innretningsnavn.
ACRONYMS = {'FPSO', 'FPU', 'FSO', 'FSU', 'PDQ', 'QP', 'DP', 'PH', 'PP', 'UPP', 'LQ', 'CDP', 'WHP',
            'LNG', 'TLP', 'FTP', 'OLS', 'SPM', 'UMS', 'FPF', 'PU', 'RP', 'ST', 'NCS', 'CO2', 'ED', 'GBS', 'SPAR'}


def title(value):
    """Sodir skriver navn i store bokstaver. 'JOHAN SVERDRUP' -> 'Johan Sverdrup'."""
    if not value:
        return value
    out = []
    for word in str(value).split(' '):
        # Behold blokk-/lisensbetegnelser som 'PL001', '25/8-4', forkortelser og enkeltbokstaver.
        if any(c.isdigit() for c in word) or word.upper() in ACRONYMS or len(word) == 1:
            out.append(word)
        else:
            out.append('-'.join(p[:1].upper() + p[1:].lower() for p in word.split('-')))
    return ' '.join(out)


# ── Eierskap ────────────────────────────────────────────────────────────────
def shares(rows, key, company, share):
    """Bygger {id: [[selskap, andel], ...]} sortert på synkende andel."""
    out = {}
    for r in rows:
        p = r['properties']
        out.setdefault(p[key], []).append([p[company], num(p[share]) or 0])
    for v in out.values():
        v.sort(key=lambda x: (-x[1], x[0]))
    return out


def licence_licensees():
    rows = fetch(3007, ['prlNpdidLicence', 'cmpLongName', 'prlLicenseeInterest'],
                 'prlLicenseeDateValidTo IS NULL', geometry=False)
    return shares(rows, 'prlNpdidLicence', 'cmpLongName', 'prlLicenseeInterest')


def baa_licensees():
    rows = fetch(3304, ['baaName', 'cmpLongName', 'baaLicenseeInterest'],
                 'baaLicenseeDateValidTo IS NULL', geometry=False)
    return shares(rows, 'baaName', 'cmpLongName', 'baaLicenseeInterest')


# ── Lag ─────────────────────────────────────────────────────────────────────
def feature(geom, props):
    return {'type': 'Feature', 'geometry': geom, 'properties': props}


def build_fields():
    hist = fetch(7108, ['fldNpdidField', 'cmpLongName', 'fldCompanyShare', 'fldLicenseeTo'],
                 geometry=False)
    # Gjeldende rettighetshavere har ingen sluttdato. Nedstengte felt har ingen
    # gjeldende - bruk da siste sammensetning (rader med feltets seneste sluttdato).
    last = {}
    for r in hist:
        p = r['properties']
        to = p['fldLicenseeTo'] if p['fldLicenseeTo'] is not None else float('inf')
        last[p['fldNpdidField']] = max(last.get(p['fldNpdidField'], -1), to)
    current = [r for r in hist
               if (r['properties']['fldLicenseeTo'] if r['properties']['fldLicenseeTo'] is not None
                   else float('inf')) == last[r['properties']['fldNpdidField']]]
    lic = shares(current, 'fldNpdidField', 'cmpLongName', 'fldCompanyShare')
    rows = fetch(7100, ['fldNpdidField', 'fldName', 'fldCurrentActivitySatus',
                        'fldHcType', 'cmpLongName', 'fldDiscoveryYear', 'fldMainArea'])
    out = []
    for r in rows:
        p = r['properties']
        if not r.get('geometry'):
            continue
        out.append(feature(r['geometry'], {
            'id': p['fldNpdidField'],
            'name': title(p['fldName']),
            'status': p['fldCurrentActivitySatus'],
            'hc': p['fldHcType'],
            'op': p['cmpLongName'],
            'year': p['fldDiscoveryYear'],
            'area': p['fldMainArea'],
            'lic': lic.get(p['fldNpdidField'], []),
        }))
    return out


def build_discoveries(prl_lic, baa_lic):
    rows = fetch(7000, ['dscNpdidDiscovery', 'dscName', 'dscCurrentActivityStatus',
                        'dscHcType', 'cmpLongName', 'dscDiscoveryYear', 'dscOwnerKind',
                        'dscOwnerName', 'fldName', 'nmaName'])
    # Lisensnavn -> id for å slå opp rettighetshavere for funn eid av en utvinningstillatelse.
    prl_by_name = {}
    for r in fetch(3000, ['prlNpdidLicence', 'prlName'], geometry=False):
        prl_by_name[r['properties']['prlName']] = r['properties']['prlNpdidLicence']
    out = []
    for r in rows:
        p = r['properties']
        # Funn som er en del av et felt vises via feltlaget.
        if not r.get('geometry') or p['fldName']:
            continue
        if p['dscCurrentActivityStatus'] in ('Producing', 'Shut down', 'Approved for production',
                                             'Included in other discovery'):
            continue
        if p['dscOwnerKind'] == 'PRODUCTION LICENCE':
            lic = prl_lic.get(prl_by_name.get(p['dscOwnerName']), [])
        elif p['dscOwnerKind'] == 'BUSINESS ARRANGEMENT AREA':
            lic = baa_lic.get(p['dscOwnerName'], [])
        else:
            lic = []
        out.append(feature(r['geometry'], {
            'id': p['dscNpdidDiscovery'],
            'name': p['dscName'] if any(c.isdigit() for c in p['dscName']) else title(p['dscName']),
            'status': p['dscCurrentActivityStatus'],
            'hc': p['dscHcType'] or None,
            'op': p['cmpLongName'],
            'year': p['dscDiscoveryYear'],
            'area': p['nmaName'],
            'lic': lic,
        }))
    return out


def build_licences(prl_lic):
    rows = fetch(3000, ['prlNpdidLicence', 'prlName', 'cmpLongName', 'prlDateGranted',
                        'prlMainArea', 'prlPhaseCurrent'], "prlActive = 'Y'")
    out = []
    for r in rows:
        p = r['properties']
        if not r.get('geometry'):
            continue
        out.append(feature(r['geometry'], {
            'id': p['prlNpdidLicence'],
            'name': p['prlName'],
            'op': p['cmpLongName'],
            'phase': p['prlPhaseCurrent'],
            'area': p['prlMainArea'],
            'lic': prl_lic.get(p['prlNpdidLicence'], []),
        }))
    return out


def build_wells():
    rows = fetch(5000, ['wlbNpdidWellbore', 'wlbWellboreName', 'wlbPurpose', 'wlbContent',
                        'wlbStatus', 'wlbDrillingOperator', 'wlbEntryYear', 'wlbCompletionYear',
                        'wlbDiscovery', 'wlbMainArea'],
                 "wlbWellType = 'EXPLORATION'")
    out = []
    for r in rows:
        p = r['properties']
        g = r.get('geometry')
        if not g or not g.get('coordinates'):
            continue
        out.append(feature(g, {
            'id': p['wlbNpdidWellbore'],
            'name': p['wlbWellboreName'],
            'purpose': p['wlbPurpose'],
            'content': p['wlbContent'],
            'status': p['wlbStatus'],
            'op': p['wlbDrillingOperator'],
            'year': p['wlbCompletionYear'] or p['wlbEntryYear'],
            'dsc': p['wlbDiscovery'],
            'area': p['wlbMainArea'],
        }))
    return out


def build_pipelines():
    rows = fetch(6100, ['pplNpdidPipeline', 'pplName', 'pplMapLabel', 'pplBelongsToName',
                        'pplMedium', 'pplCurrentPhase', 'pplDimension', 'cmpLongName',
                        'fclNameFrom', 'fclNameTo'])
    out = []
    for r in rows:
        p = r['properties']
        if not r.get('geometry'):
            continue
        out.append(feature(r['geometry'], {
            'id': p['pplNpdidPipeline'],
            'name': p['pplBelongsToName'] or p['pplName'],
            'label': p['pplMapLabel'],
            'medium': p['pplMedium'],
            'phase': p['pplCurrentPhase'],
            'dim': num(p['pplDimension']),
            'op': p['cmpLongName'],
            'from': p['fclNameFrom'],
            'to': p['fclNameTo'],
        }))
    return out


def build_facilities():
    rows = fetch(6000, ['fclNpdidFacility', 'fclName', 'fclKind', 'fclPhase', 'fclSurface',
                        'fclBelongsToName', 'fclCurrentOperatorName', 'fclFixedOrMoveable'],
                 "fclFixedOrMoveable = 'FIXED' AND fclPhase IN ('IN SERVICE', 'INSTALLATION')")
    out = []
    for r in rows:
        p = r['properties']
        if not (r.get('geometry') or {}).get('coordinates') or p['fclKind'] in ('LANDFALL', 'OFFSHORE WIND TURBINE'):
            continue
        if p['fclKind'] == 'ONSHORE FACILITY':
            cls = 'onshore'
        elif p['fclSurface'] == 'N':
            cls = 'subsea'
        else:
            cls = 'surface'
        out.append(feature(r['geometry'], {
            'id': p['fclNpdidFacility'],
            'name': title(p['fclName']),
            'kind': p['fclKind'],
            'cls': cls,
            'phase': p['fclPhase'],
            'field': p['fclBelongsToName'],
            'op': p['fclCurrentOperatorName'],
        }))
    return out


def build_quadrants():
    return [feature(r['geometry'], {'name': r['properties']['qadName']})
            for r in fetch(1002, ['qadName']) if r.get('geometry')]


def build_blocks():
    return [feature(r['geometry'], {'name': r['properties']['blcName']})
            for r in fetch(1001, ['blcName']) if r.get('geometry')]


# ── Skriving ────────────────────────────────────────────────────────────────
def write(name, features):
    features.sort(key=lambda f: (str(f['properties'].get('id', '')), f['properties'].get('name') or ''))
    path = os.path.join(OUT_DIR, f'{name}.json')
    body = json.dumps({'type': 'FeatureCollection', 'features': features},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    old = None
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            old = fh.read()
    if old == body:
        print(f'  {path}: uendret')
        return False
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(body)
    print(f'  {path}: {len(features)} features, {len(body) // 1024} KB')
    return True


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print('Henter eierskap ...')
    prl_lic = licence_licensees()
    baa_lic = baa_licensees()

    layers = {
        'fields': build_fields,
        'discoveries': lambda: build_discoveries(prl_lic, baa_lic),
        'licences': lambda: build_licences(prl_lic),
        'wells': build_wells,
        'pipelines': build_pipelines,
        'facilities': build_facilities,
        'quadrants': build_quadrants,
        'blocks': build_blocks,
    }
    changed = False
    for name, build in layers.items():
        print(f'Henter {name} ...')
        features = build()
        if not features:
            # Et tomt svar betyr nesten alltid at Sodir har problemer - behold gamle data.
            print(f'  ! ingen features for {name} - beholder eksisterende fil', file=sys.stderr)
            continue
        changed |= write(name, features)

    meta_path = os.path.join(OUT_DIR, 'meta.json')
    if changed or not os.path.exists(meta_path):
        with open(meta_path, 'w', encoding='utf-8') as fh:
            json.dump({'updated': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                       'source': SOURCE}, fh, ensure_ascii=False, indent=2)
        print('Oppdaterte meta.json')
    else:
        print('Ingen endringer.')


if __name__ == '__main__':
    main()
