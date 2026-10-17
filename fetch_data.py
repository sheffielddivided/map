"""
Henter kartdata for norsk, britisk og dansk sokkel og lagrer dem som kompakte
GeoJSON-filer under data/ for ppt.html:

- Norge: Sodir (Norwegian Offshore Directorate) FactMaps
- Storbritannia: NSTA (North Sea Transition Authority) Open Data, ArcGIS
- Danmark: GEUS sin WFS-tjeneste for Energistyrelsens data (feltavgrensninger,
  rørledninger, brønnresultater), supplert med GeoJSON bygget fra ENS sine
  shapefiler i github.com/sheffielddivided/ens (lisenser, brønner,
  innretninger, blokker, eierandeler og produksjon)

Alle features får egenskapen cc ('no', 'uk' eller 'dk'), og selskapsnavn
normaliseres til konsernnavn (se GROUPS), slik at ett valg i selskapsfilteret
dekker f.eks. både «Equinor Energy AS» og «EQUINOR UK LIMITED».

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
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

BASE = 'https://factmaps.sodir.no/api/rest/services/DataService/Data/MapServer'
NSTA = 'https://services-eu1.arcgis.com/OZMfUznmLTnWccBc/arcgis/rest/services'
ENS_RAW = 'https://raw.githubusercontent.com/sheffielddivided/ens/main/docs/data'
# GEUS sin WFS-tjeneste for Energistyrelsens data (lenket fra ENS sin shapefil-side), i WGS84.
GEUS_WFS = 'https://data.geus.dk/geusmap/ows/4326.jsp'
OUT_DIR = 'data'
PAGE_SIZE = 2000
TIMEOUT = 180
RETRIES = 4
# Geometriforenkling i grader (ca. 50 m nord-sør) og antall desimaler (~10 m).
SIMPLIFY = 0.0005
PRECISION = 4

SOURCE = ('Sodir FactMaps (NLOD 2.0); NSTA Open Data (Open Government Licence v3.0); '
          'Energistyrelsen via GEUS WFS og github.com/sheffielddivided/ens')


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


def nsta(service, layer=0):
    return f'{NSTA}/{urllib.parse.quote(service)}/FeatureServer/{layer}'


def fetch(layer, out_fields, where='1=1', geometry=True):
    """Henter alle rader fra et lag (Sodir-lagnummer eller full URL), paginert og sortert på OBJECTID."""
    url = layer if isinstance(layer, str) else f'{BASE}/{layer}'
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
        data = _get(f'{url}/query', params)
        page = data.get('features', [])
        if not geometry:
            page = [{'properties': f['attributes']} for f in page]
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    print(f'  {str(layer).split("/services/")[-1]}: {len(rows)} rader')
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


# ── Britisk sektor (NSTA) ───────────────────────────────────────────────────
UK_FIELDS = nsta('Offshore_hydrocarbon_fields_(WGS84)')
UK_DISCOVERIES = nsta('UKCS_offshore_petroleum_undeveloped_discoveries_(WGS84)')
UK_WELLS = nsta('UKCS_offshore_wellbore_top_holes_(WGS84)')
UK_PIPELINES = nsta('UKCS_offshore_infrastructure_pipeline_linear_(WGS84)', 1)
UK_SURFACE = nsta('UKCS offshore infrastructure surface points WGS84', 1)
UK_SUBSEA = nsta('UKCS_offshore_infrastructure_subsea_points_(WGS84)', 1)
UK_LICENCES = nsta('UKCS offshore petroleum licence subareas by equity group holder WGS84')
UK_QUADRANTS = nsta('UKCS_quadrants_(WGS84)')
UK_BLOCKS = nsta('UKCS_blocks_(WGS84)')

# NSTA-status -> samme vokabular som Sodir. Andre statuser (skrinlagt, under avgrensning) tas ikke med.
UK_FIELD_STATUS = {
    'PRODUCING': 'Producing',
    'CONSTRUCTION-FDP APPROV': 'Approved for production',
    'PRODUCTION CEASED': 'Shut down', 'POST-COP': 'Shut down',
    'PRODUCTION CEASED - VENT CONSENT NEEDED': 'Shut down',
    'PRODUCTION SUSPENDED': 'Shut down', 'PRODUCTION SUSPENDED - POSSIBLE RESERVES REMAIN': 'Shut down',
}
UK_FIELD_HC = {'OIL': 'OIL', 'GAS': 'GAS', 'COND': 'GAS/CONDENSATE'}
UK_DSC_HC = {'O': 'OIL', 'G': 'GAS', 'GC': 'GAS/CONDENSATE', 'O&G': 'OIL/GAS', 'G&O': 'OIL/GAS'}
UK_PIPE_MEDIUM = {'GAS': 'Gas', 'OIL': 'Oil', 'CONDENSATE': 'Condensate', 'MIXED HYDROCARBONS': 'Mixed'}
# Bare transportrør (som Sodirs rørlag): aktive hydrokarbonrør fra 16 tommer og oppover.
UK_PIPE_MIN_MM = 406


def year_of(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).year
    except (TypeError, ValueError, OSError):
        return None


def uk_well_content(fluids):
    f = {x.strip().upper() for x in (fluids or '').split(',') if x.strip()}
    if not f:
        return None
    if 'OIL' in f and 'GAS' in f:
        return 'OIL/GAS'
    if 'OIL' in f:
        return 'OIL'
    if 'GAS' in f or 'CONDENSATE' in f:
        return 'GAS/CONDENSATE' if 'CONDENSATE' in f else 'GAS'
    return 'WATER' if 'WATER' in f else None


def build_uk_fields():
    lic_fields = []
    for i in range(1, 21):
        lic_fields += ['CURRLIC' if i == 1 else f'CURRLIC{i}', 'EQUITY' if i == 1 else f'EQUITY{i}']
    rows = fetch(UK_FIELDS, ['FIELDNAME', 'STATUS', 'FIELDTYPE', 'CURR_OPER', 'DISC_DATE'] + lic_fields)
    merged = {}
    for r in rows:
        p = r['properties']
        status = UK_FIELD_STATUS.get(p['STATUS'])
        if not status or not r.get('geometry') or not p['FIELDNAME']:
            continue
        lic = []
        for i in range(1, 21):
            name = p['CURRLIC' if i == 1 else f'CURRLIC{i}']
            if name:
                lic.append([name, num(p['EQUITY' if i == 1 else f'EQUITY{i}']) or 0])
        key = p['FIELDNAME']
        if key in merged:
            # Samme felt kan ha flere polygoner (delområder) - slå dem sammen.
            merged[key]['geometry'] = merge_geoms(merged[key]['geometry'], r['geometry'])
            continue
        merged[key] = feature(r['geometry'], {
            'id': 'uk-' + slug(key), 'cc': 'uk', 'name': title(key), 'status': status,
            'hc': UK_FIELD_HC.get(p['FIELDTYPE']), 'op': p['CURR_OPER'], 'year': year_of(p['DISC_DATE']),
            'area': None, 'lic': lic,
        })
    return list(merged.values())


def build_uk_discoveries():
    rows = fetch(UK_DISCOVERIES, ['DISC_CODE', 'DISC_NAME', 'WELLREGNO', 'HC_TYPE'])
    out = []
    for r in rows:
        p = r['properties']
        if not (r.get('geometry') or {}).get('coordinates'):
            continue
        name = p['DISC_NAME'] if p['DISC_NAME'] and p['DISC_NAME'].strip() else p['WELLREGNO']
        out.append(feature(r['geometry'], {
            'id': f"uk-{p['DISC_CODE'] or slug(name)}", 'cc': 'uk', 'name': title(name),
            'status': 'Undeveloped', 'hc': UK_DSC_HC.get(p['HC_TYPE']), 'op': None, 'year': None,
            'area': None, 'lic': [],
        }))
    return out


def build_uk_licences():
    rows = fetch(UK_LICENCES, ['SUBAREAID', 'LICREF', 'BLOCKREF', 'OPORGGRP', 'EQGRPHOLD', 'EQUITY'])
    subs = {}
    for r in rows:
        p = r['properties']
        if not r.get('geometry'):
            continue
        sub = subs.setdefault(p['SUBAREAID'], feature(r['geometry'], {
            'id': f"uk-{p['SUBAREAID']}", 'cc': 'uk', 'name': p['LICREF'], 'op': p['OPORGGRP'],
            'phase': None, 'area': p['BLOCKREF'], 'lic': [],
        }))
        if p['EQGRPHOLD']:
            sub['properties']['lic'].append([p['EQGRPHOLD'], num(p['EQUITY']) or 0])
    return list(subs.values())


def build_uk_wells():
    rows = fetch(UK_WELLS, ['WELLREGNO', 'CURRWELLIN', 'FLUIDTYPES', 'SPUDDATE', 'COMPLEDATE', 'TDOPGRP', 'SUBOPGRP'],
                 "CURRWELLIN IN ('Exploration','Appraisal') AND WELLREGNO = WELLORIGIN")
    out = []
    for r in rows:
        p = r['properties']
        if not (r.get('geometry') or {}).get('coordinates'):
            continue
        op = p['TDOPGRP'] if p['TDOPGRP'] and p['TDOPGRP'] != 'No Data Available' else p['SUBOPGRP']
        out.append(feature(r['geometry'], {
            'id': 'uk-' + ' '.join(p['WELLREGNO'].split()), 'cc': 'uk', 'name': ''.join(p['WELLREGNO'].split()),
            'purpose': 'WILDCAT' if p['CURRWELLIN'] == 'Exploration' else 'APPRAISAL',
            'content': uk_well_content(p['FLUIDTYPES']), 'status': None,
            'op': op if op and op != 'No Data Available' else None,
            'year': year_of(p['COMPLEDATE']) or year_of(p['SPUDDATE']), 'dsc': None, 'area': None,
        }))
    return out


def build_uk_pipelines():
    rows = fetch(UK_PIPELINES, ['FEATURE_ID', 'PIPE_NAME', 'FLUID', 'DIAMETERMM', 'REP_GROUP', 'DESCRIPTIO'],
                 "INF_TYPE = 'PIPELINE' AND STATUS = 'ACTIVE' AND FLUID IN ('GAS','OIL','CONDENSATE','MIXED HYDROCARBONS') "
                 f"AND DIAMETERMM >= {UK_PIPE_MIN_MM}")
    out = []
    for r in rows:
        p = r['properties']
        if not r.get('geometry'):
            continue
        out.append(feature(r['geometry'], {
            'id': f"uk-{p['FEATURE_ID']}", 'cc': 'uk', 'name': p['PIPE_NAME'] or p['DESCRIPTIO'], 'label': None,
            'medium': UK_PIPE_MEDIUM.get(p['FLUID']), 'phase': 'IN SERVICE',
            'dim': round(p['DIAMETERMM'] / 25.4, 1) if p['DIAMETERMM'] else None,
            'op': p['REP_GROUP'], 'from': None, 'to': None,
        }))
    return out


def build_uk_facilities():
    out = []
    for r in fetch(UK_SURFACE, ['FEATURE_ID', 'NAME', 'INF_TYPE', 'REP_GROUP'],
                   "STATUS = 'ACTIVE' AND INF_TYPE IN ('PLATFORM','FPSO','FSO','TERMINAL')"):
        p = r['properties']
        if not (r.get('geometry') or {}).get('coordinates'):
            continue
        out.append(feature(r['geometry'], {
            'id': f"uk-{p['FEATURE_ID']}", 'cc': 'uk', 'name': title(p['NAME']), 'kind': p['INF_TYPE'],
            'cls': 'onshore' if p['INF_TYPE'] == 'TERMINAL' else 'surface', 'phase': 'IN SERVICE',
            'field': None, 'op': p['REP_GROUP'] if p['REP_GROUP'] != 'unknown' else None,
        }))
    for r in fetch(UK_SUBSEA, ['FEATURE_ID', 'NAME', 'INF_TYPE', 'REP_GROUP'],
                   "STATUS = 'ACTIVE' AND INF_TYPE IN ('TEMPLATE','MANIFOLD')"):
        p = r['properties']
        if not (r.get('geometry') or {}).get('coordinates'):
            continue
        out.append(feature(r['geometry'], {
            'id': f"uk-{p['FEATURE_ID']}", 'cc': 'uk', 'name': title(p['NAME']), 'kind': p['INF_TYPE'],
            'cls': 'subsea', 'phase': 'IN SERVICE', 'field': None, 'op': p['REP_GROUP'],
        }))
    return out


def build_uk_quadrants():
    return [feature(r['geometry'], {'name': r['properties']['QUADRANT'], 'cc': 'uk'})
            for r in fetch(UK_QUADRANTS, ['QUADRANT']) if r.get('geometry')]


def build_uk_blocks():
    return [feature(r['geometry'], {'name': r['properties']['BLOCK_REF'], 'cc': 'uk'})
            for r in fetch(UK_BLOCKS, ['BLOCK_REF']) if r.get('geometry')]


# ── Dansk sektor (ENS via sheffielddivided/ens) ─────────────────────────────
_ens_cache = {}


def ens(path):
    if path not in _ens_cache:
        last = None
        for attempt in range(RETRIES):
            try:
                req = urllib.request.Request(f'{ENS_RAW}/{path}', headers={'User-Agent': 'ppt-map-fetch'})
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    _ens_cache[path] = json.loads(resp.read().decode('utf-8'))
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < RETRIES - 1:
                    time.sleep(2 ** (attempt + 1))
        else:
            raise RuntimeError(f'ens/{path}: {last}')
        print(f'  ens/{path}')
    return _ens_cache[path]


def round_geom(g):
    def rc(c):
        return [round(c[0], PRECISION), round(c[1], PRECISION)] if isinstance(c[0], (int, float)) else [rc(x) for x in c]
    return {'type': g['type'], 'coordinates': rc(g['coordinates'])}


def dk_field_status():
    """Produserende = produksjon siste 12 måneder (månedsdata) eller i siste år (årsdata).
    Felt med tidligere produksjon er nedstengt; felt uten produksjon er godkjent for utbygging."""
    produced = lambda r: (r.get('oil') or 0) > 0 or (r.get('gas') or 0) > 0  # noqa: E731
    monthly = ens('monthly.json')['records']
    yearly = ens('yearly.json')['records']
    latest_m = max(r['year'] * 12 + r['month'] for r in monthly)
    latest_y = max(r['year'] for r in yearly)
    status = {}
    for r in yearly + monthly:
        if produced(r):
            status.setdefault(r['field'], 'Shut down')
    for r in monthly:
        if produced(r) and r['year'] * 12 + r['month'] > latest_m - 12:
            status[r['field']] = 'Producing'
    for r in yearly:
        if produced(r) and r['year'] == latest_y:
            status[r['field']] = 'Producing'
    return status


def dk_ownership():
    own = ens('ownership.json')['fields']
    return {slug_: sorted([[c, round(v * 100, 4)] for c, v in shares_.items()], key=lambda x: (-x[1], x[0]))
            for slug_, shares_ in own.items()}


# GEUS leverer GML2 (ikke GeoJSON); det leses med standardbiblioteket.
_GML = {'gml': 'http://www.opengis.net/gml', 'ms': 'http://mapserver.gis.umn.edu/mapserver'}


def _gml_coords(el):
    return [[round(float(v), PRECISION) for v in p.split(',')[:2]] for p in el.text.split()]


def _gml_geom(g):
    tag = g.tag.split('}')[1]
    c = lambda e: _gml_coords(e.find('gml:coordinates', _GML))  # noqa: E731
    if tag == 'Point':
        return {'type': 'Point', 'coordinates': c(g)[0]}
    if tag == 'LineString':
        return {'type': 'LineString', 'coordinates': c(g)}
    if tag == 'MultiLineString':
        return {'type': 'MultiLineString', 'coordinates': [c(ls) for ls in g.iter('{%s}LineString' % _GML['gml'])]}

    def poly(pg):
        rings = [_gml_coords(pg.find('gml:outerBoundaryIs/gml:LinearRing/gml:coordinates', _GML))]
        return rings + [_gml_coords(r) for r in pg.findall('gml:innerBoundaryIs/gml:LinearRing/gml:coordinates', _GML)]
    if tag == 'Polygon':
        return {'type': 'Polygon', 'coordinates': poly(g)}
    if tag == 'MultiPolygon':
        return {'type': 'MultiPolygon', 'coordinates': [poly(pg) for pg in g.iter('{%s}Polygon' % _GML['gml'])]}
    raise ValueError(f'ukjent GML-geometri {tag}')


def geus(typename):
    """Henter et WFS-lag fra GEUS som liste av {'geometry', 'properties'}."""
    params = {'nocache': 'nocache', 'whoami': 'ul@geus.dk', 'SERVICE': 'WFS', 'VERSION': '1.0.0',
              'REQUEST': 'GetFeature', 'TYPENAME': typename, 'outputformat': 'gml2'}
    url = f'{GEUS_WFS}?{urllib.parse.urlencode(params)}'
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'ppt-map-fetch'})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                root = ET.fromstring(resp.read())
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < RETRIES - 1:
                time.sleep(2 ** (attempt + 1))
    else:
        raise RuntimeError(f'GEUS {typename}: {last}')
    out = []
    for member in root.findall('gml:featureMember', _GML):
        f = list(member)[0]
        props, geom = {}, None
        for child in f:
            name = child.tag.split('}')[1]
            if name == 'msGeometry':
                geom = _gml_geom(list(child)[0])
            elif name != 'boundedBy':
                props[name] = child.text
        out.append({'geometry': geom, 'properties': props})
    print(f'  GEUS {typename}: {len(out)} rader')
    return out


# GEUS-feltnavn -> slug i ens-datasettet (eierandeler og produksjon er nøklet på slug).
# Igor og Sif regnes som Halfdan, slik ENS gjør i produksjonstallene.
DK_FIELD_SLUG = {'igor': 'halfdan', 'sif': 'halfdan', 'tyra se': 'tyra_se', 'syd arne': 'syd_arne'}


def dk_slug(field_name):
    low = field_name.strip().lower()
    return DK_FIELD_SLUG.get(low, low.replace(' ', '_'))


def build_dk_fields():
    meta = ens('fields.json')['fields']
    status = dk_field_status()
    own = dk_ownership()

    def props(sl, fallback_name):
        m = meta.get(sl, {})
        return {
            'id': 'dk-' + sl, 'cc': 'dk', 'name': fallback_name or m.get('display_name'),
            # Felt uten produksjon i ENS-dataene har en godkjent avgrensning, men er ikke i produksjon ennå.
            'status': status.get(sl, 'Approved for production'),
            'hc': None, 'op': m.get('operator'), 'year': m.get('first_year'), 'area': None, 'lic': own.get(sl, []),
        }

    merged = {}

    def add(sl, name, geom):
        if sl in merged:
            merged[sl]['geometry'] = merge_geoms(merged[sl]['geometry'], geom)
        else:
            merged[sl] = feature(geom, props(sl, name))

    try:
        for r in geus('samba_fields'):
            name = r['properties'].get('field_name')
            if name and r['geometry']:
                add(dk_slug(name), 'Halfdan' if dk_slug(name) == 'halfdan' else name, r['geometry'])
    except Exception as exc:  # noqa: BLE001
        print(f'  ! GEUS-felt feilet ({exc}) - bruker bare ENS-shapefilen', file=sys.stderr)
    # Felt som bare finnes i ENS-shapefilen (f.eks. Lulita), eller alle hvis GEUS feilet.
    from_geus = set(merged)
    for f in ens('gis/fields.geojson')['features']:
        p = f['properties']
        if p['slug'] not in from_geus:
            add(p['slug'], p['name'].split(' - ')[0].split(' (')[0], round_geom(f['geometry']))
    return list(merged.values())


DK_PIPE_MEDIUM = {'Gas': 'Gas', 'Oil': 'Oil', 'Multi-phase': 'Mixed'}


def build_dk_pipelines():
    merged = {}
    for r in geus('pipelines_simplified'):
        p, g = r['properties'], r['geometry']
        if not g:
            continue
        lines = [g['coordinates']] if g['type'] == 'LineString' else g['coordinates']
        name = p.get('name') or '?'
        key = (name, p.get('content_name'))
        if key in merged:
            merged[key]['geometry']['coordinates'] += lines
            continue
        merged[key] = feature({'type': 'MultiLineString', 'coordinates': lines}, {
            'id': f'dk-{slug(name)}-{slug(p.get("content_name") or "")}', 'cc': 'dk', 'name': name, 'label': None,
            'medium': DK_PIPE_MEDIUM.get(p.get('content_name')), 'phase': 'IN SERVICE', 'dim': None,
            'op': None, 'from': None, 'to': None,
        })
    return list(merged.values())


# GEUS-brønnstatus -> Sodirs innholdsvokabular (brukes til «farge etter resultat»).
DK_WELL_CONTENT = {
    'Dry': 'DRY', 'Oil': 'OIL', 'Gas': 'GAS', 'Oil/gas': 'OIL/GAS', 'Gas/condensate': 'GAS/CONDENSATE',
    'Oil w. gas shows': 'OIL', 'Gas w. oil shows': 'GAS', 'Show oil': 'OIL SHOWS', 'Show gas': 'GAS SHOWS',
    'Show oil/gas': 'OIL/GAS SHOWS', 'Show': 'SHOWS',
}


def dk_well_results():
    """Brønnnavn (store bokstaver, også sidesteg) -> innhold, fra GEUS."""
    out = {}
    try:
        for r in geus('samba_wellbores'):
            p = r['properties']
            content = DK_WELL_CONTENT.get(p.get('status'))
            if not content:
                continue
            names = [p.get('well_name') or ''] + (p.get('segment_names') or '').split(',')
            for n in names:
                if n.strip():
                    out.setdefault(n.strip().upper(), content)
    except Exception as exc:  # noqa: BLE001
        print(f'  ! GEUS-brønnresultater feilet ({exc}) - brønnene får ukjent resultat', file=sys.stderr)
    return out


def build_dk_licences():
    own = dk_ownership()
    out = []
    for i, f in enumerate(ens('gis/licences.geojson')['features']):
        p = f['properties']
        lic_name = p.get('LICENCE') or ''
        out.append(feature(round_geom(f['geometry']), {
            'id': f'dk-{slug(lic_name)}-{i}', 'cc': 'dk', 'name': lic_name,
            'op': p.get('Operator_1') or p.get('OPERATOR'), 'phase': None, 'area': None,
            # DUC-områdene eies av hovedkonsesjonen; samme andeler som Dan-feltet.
            'lic': own.get('dan', []) if lic_name == 'DUC' else [],
        }))
    return out


def build_dk_wells():
    results = dk_well_results()
    out = []
    for f in ens('gis/wells.geojson')['features']:
        p = f['properties']
        cls = (p.get('Classifica') or '').upper()
        if p.get('Location') == 'ONSHORE' or not (cls.startswith('EXPLORATION') or cls.startswith('APPRAISAL')):
            continue
        date = p.get('Comp_Date') or p.get('Spud_Date') or ''
        out.append(feature(round_geom(f['geometry']), {
            'id': 'dk-' + (p.get('Well_Numb') or p.get('Well_Name')), 'cc': 'dk',
            'name': p.get('Well_Numb') or p.get('Well_Name'),
            'purpose': 'WILDCAT' if cls.startswith('EXPLORATION') else 'APPRAISAL',
            'content': results.get((p.get('Well_Name') or '').upper()), 'status': None,
            'op': p.get('Operator'), 'year': int(date[:4]) if date[:4].isdigit() else None, 'dsc': None, 'area': None,
        }))
    return out


def build_dk_facilities():
    out = []
    for f in ens('gis/installations.geojson')['features']:
        p = f['properties']
        if p.get('Current_St') not in ('Operational', 'U. construction'):
            continue
        out.append(feature(round_geom(f['geometry']), {
            'id': 'dk-' + p['ID'], 'cc': 'dk', 'name': p['Name'], 'kind': p.get('Category'),
            'cls': 'subsea' if p.get('Function') == 'Subsea' else 'surface',
            'phase': 'IN SERVICE', 'field': None, 'op': p.get('Operator'),
        }))
    return out


def build_dk_blocks():
    return [feature(round_geom(f['geometry']), {'name': f['properties']['BlockNo'], 'cc': 'dk'})
            for f in ens('gis/blocks.geojson')['features']]


# ── Hjelpere ────────────────────────────────────────────────────────────────
def slug(value):
    return ''.join(c if c.isalnum() else '-' for c in str(value).lower()).strip('-')


def merge_geoms(a, b):
    polys = []
    for g in (a, b):
        polys += [g['coordinates']] if g['type'] == 'Polygon' else g['coordinates']
    return {'type': 'MultiPolygon', 'coordinates': polys}


# ── Selskapsgrupper ─────────────────────────────────────────────────────────
# Konsernnavn og mønstre (starten av navnet, små bokstaver). Første treff vinner,
# så mer spesifikke mønstre må stå før generelle (f.eks. «aker bp» før «bp»).
GROUPS = [
    ('Aker BP', ['aker bp']), ('Equinor', ['equinor', 'statoil']), ('Vår Energi', ['vår energi', 'var energi']),
    ('Petoro', ['petoro']), ('TotalEnergies', ['totalenergies', 'total e&p', 'total ep', 'total ']),
    ('ConocoPhillips', ['conocophillips']), ('Harbour Energy', ['harbour', 'chrysaor', 'premier oil']), ('INEOS', ['ineos']),
    ('Shell', ['shell', 'a/s norske shell']), ('BP', ['bp ', 'britoil', 'amoco']), ('ExxonMobil', ['exxonmobil', 'esso', 'mobil ']),
    ('DNO', ['dno']), ('OKEA', ['okea']), ('ORLEN', ['orlen', 'pgnig']), ('Wintershall Dea', ['wintershall']),
    ('OMV', ['omv']), ('INPEX Idemitsu', ['inpex', 'idemitsu']), ('Repsol', ['repsol']), ('Sval Energi', ['sval']),
    ('Neptune Energy', ['neptune']), ('Chevron', ['chevron']), ('CNOOC', ['cnooc', 'nexen']),
    ('Ithaca Energy', ['ithaca']), ('Serica Energy', ['serica']), ('EnQuest', ['enquest']), ('Apache', ['apache', 'apa ']),
    ('Perenco', ['perenco']), ('TAQA', ['taqa']), ('Spirit Energy', ['spirit energy']), ('Dana Petroleum', ['dana petroleum']),
    ('NEO NEXT', ['neo next', 'neo energy']), ('CNR International', ['cnr international', 'canadian natural']),
    ('Eni', ['eni ', 'eni uk']), ('Hess', ['hess']), ('Kufpec', ['kufpec']), ('Japex', ['japex']),
    ('Lime Petroleum', ['lime petroleum']), ('Concedo', ['concedo']), ('Petrolia', ['petrolia']),
    ('BlueNord', ['bluenord', 'noreco', 'norwegian energy']), ('Nordsøfonden', ['nordsøfonden', 'nordsofonden']),
    ('Danoil', ['danoil']), ('Adura', ['adura']), ('Hibiscus', ['hibiscus', 'anasuria']), ('Energean', ['energean']),
    ('Tailwind', ['tailwind']), ('Waldorf', ['waldorf']), ('Viaro', ['viaro', 'rockrose']), ('Tullow', ['tullow']), ('Jersey Oil & Gas', ['jersey oil']),
    ('Kistos', ['kistos']), ('Centrica', ['centrica']), ('One-Dyas', ['one-dyas', 'one dyas']),
    ('Pandion Energy', ['pandion']), ('M Vest Energy', ['m vest']), ('Longboat', ['longboat']),
    ('Source Energy', ['source energy']), ('Vermilion', ['vermilion']), ('Mitsui', ['mitsui']), ('Sumitomo', ['sumitomo']),
]
_LEGAL = {'as', 'asa', 'a/s', 'aps', 'ab', 'limited', 'ltd', 'ltd.', 'plc', 'b.v.', 'bv', 'gmbh', 's.a.', 'inc', 'inc.',
          'norge', 'norway', 'uk', '(uk)', 'u.k.', '(u.k.)', 'danmark', 'denmark', 'corporation', 'company', 'group'}


def group(name):
    if not name or name.strip().lower() in ('no operator', 'unknown', 'no data available'):
        return None
    low = name.strip().lower() + ' '
    for display, prefixes in GROUPS:
        if any(low.startswith(px) for px in prefixes):
            return display
    # Ukjent selskap: fjern juridiske endelser og landnavn, og pynt STORE BOKSTAVER.
    words = name.strip().split()
    while len(words) > 1 and words[-1].lower() in _LEGAL:
        words.pop()
    out = ' '.join(words)
    return title(out) if out.isupper() else out


ALIASES = {}


def regroup(features):
    """Erstatter operatør og rettighetshavere med konsernnavn og summerer andeler per konsern."""
    for f in features:
        p = f['properties']
        for k in ('op',):
            if p.get(k):
                g = group(p[k])
                if g:
                    ALIASES[p[k]] = g
                p[k] = g
        if 'lic' in p:
            agg = {}
            for name, share in p['lic']:
                g = group(name)
                if not g:
                    continue
                ALIASES[name] = g
                agg[g] = round(agg.get(g, 0) + (share or 0), 4)
            p['lic'] = sorted(([g, v] for g, v in agg.items()), key=lambda x: (-x[1], x[0]))
        p.setdefault('cc', 'no')
    return features


# ── Skriving ────────────────────────────────────────────────────────────────
def write(name, features):
    features.sort(key=lambda f: (f['properties'].get('cc', 'no'), str(f['properties'].get('id', '')), f['properties'].get('name') or ''))
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


def previous(name, cc):
    """Features for ett land fra forrige kjøring - brukes hvis kilden feiler."""
    path = os.path.join(OUT_DIR, f'{name}.json')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as fh:
        return [f for f in json.load(fh)['features'] if f['properties'].get('cc', 'no') == cc]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print('Henter eierskap ...')
    prl_lic = licence_licensees()
    baa_lic = baa_licensees()

    layers = {
        'fields': {'no': build_fields, 'uk': build_uk_fields, 'dk': build_dk_fields},
        'discoveries': {'no': lambda: build_discoveries(prl_lic, baa_lic), 'uk': build_uk_discoveries},
        'licences': {'no': lambda: build_licences(prl_lic), 'uk': build_uk_licences, 'dk': build_dk_licences},
        'wells': {'no': build_wells, 'uk': build_uk_wells, 'dk': build_dk_wells},
        'pipelines': {'no': build_pipelines, 'uk': build_uk_pipelines, 'dk': build_dk_pipelines},
        'facilities': {'no': build_facilities, 'uk': build_uk_facilities, 'dk': build_dk_facilities},
        'quadrants': {'no': build_quadrants, 'uk': build_uk_quadrants},
        'blocks': {'no': build_blocks, 'uk': build_uk_blocks, 'dk': build_dk_blocks},
    }
    changed = False
    for name, sources in layers.items():
        print(f'Henter {name} ...')
        features = []
        for cc, build in sources.items():
            try:
                part = build()
            except Exception as exc:  # noqa: BLE001 - én kilde som feiler skal ikke stoppe de andre
                print(f'  ! {name}/{cc} feilet: {exc}', file=sys.stderr)
                part = []
            if not part:
                # Et tomt svar betyr nesten alltid problemer hos kilden - behold forrige data for landet.
                part = previous(name, cc)
                print(f'  ! ingen features for {name}/{cc} - beholder {len(part)} fra forrige kjøring', file=sys.stderr)
            for f in part:
                f['properties']['cc'] = cc
            features += part
        if not features:
            continue
        changed |= write(name, regroup(features))

    alias_path = os.path.join(OUT_DIR, 'companies.json')
    old_aliases = {}
    if os.path.exists(alias_path):
        with open(alias_path, encoding='utf-8') as fh:
            old_aliases = json.load(fh)
    # Behold gamle navn (de kan ligge i lagrede lenker og maler).
    aliases = {**old_aliases, **{k: v for k, v in ALIASES.items() if k != v}}
    if aliases != old_aliases:
        with open(alias_path, 'w', encoding='utf-8') as fh:
            json.dump(aliases, fh, ensure_ascii=False, sort_keys=True, indent=0)
        changed = True

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
