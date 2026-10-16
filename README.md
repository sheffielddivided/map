# map
Aker BP map

## PPT-kart (`ppt.html`)

Lager egendefinerte kart over norsk sokkel til PowerPoint-presentasjoner.

- **Kartlag:** felt i produksjon, felt under utbygging, nedstengte felt, funn, aktive lisenser,
  transportrørledninger, innretninger og letebrønner (filtrert på år, formål og resultat).
- **Selskapsfilter:** velg ett eller flere selskaper som operatør og/eller partner. De kan fremheves
  (andre vises i grått) eller vises alene, med valgfri eierandel i etiketten. Gjelder felt, funn og lisenser.
- **Bakgrunn:** vektorkart med kystlinje, dybdekurver, landegrenser, byer og kvadrant-/blokknett.
  Fire fargetemaer pluss egne farger, og transparent bakgrunn.
- **Slide-format:** 16:9, halvside, 4:3, kvadrat, stående eller egendefinert. Rammen viser nøyaktig
  det som eksporteres.
- **Eksport:** *Kopier* legger en PNG på utklippstavlen (lim inn i PowerPoint med Ctrl+V), *PNG*
  laster ned i opptil 8K, og *SVG* gir vektorgrafikk. I PowerPoint kan SVG-en konverteres til figur
  ved å høyreklikke og velge «Konverter til figur».
- **Etiketter:** overlappende etiketter skjules automatisk. Dra en etikett for å flytte den, og
  dobbeltklikk for å skjule den.
- **Språk:** norsk eller engelsk, både i grensesnittet og i kartet (tegnforklaring, kildehenvisning, verktøytips).
- **Lagring:** hele oppsettet ligger i URL-en, så kart kan bokmerkes og deles. Navngitte maler
  lagres lokalt i nettleseren.

### Data

`data/*.json` hentes fra [Sodir FactMaps](https://factmaps.sodir.no) (NLOD 2.0) med
`fetch_data.py`, som kun bruker Python-standardbiblioteket. GitHub Action-en
`.github/workflows/update-map-data.yml` kjører skriptet daglig og committer bare når dataene
faktisk er endret. Skriptet kan også kjøres lokalt:

```
python fetch_data.py
```

`data/basemap.json` (kystlinje, landegrenser og dybdekurver) er statisk. Den er bygget én gang fra
[Natural Earth](https://www.naturalearthdata.com) 10m (public domain) med
[mapshaper](https://github.com/mbloch/mapshaper):

```
mapshaper -i ne_10m_land.shp ne_10m_minor_islands.shp combine-files -merge-layers force \
  -clip bbox=-14,48,50,83 -dissolve2 -simplify 70% weighted keep-shapes \
  -filter-slivers min-area=0.3km2 -rename-layers land -o land.json format=topojson
# tilsvarende for admin_0_boundary_lines_land (borders) og bathymetry_K_200/J_1000/I_2000/H_3000
# (d200/d1000/d2000/d3000), deretter:
mapshaper -i land.json borders.json d200.json d1000.json d2000.json d3000.json combine-files \
  -o basemap.json format=topojson quantization=100000
```

Siden må serveres over HTTP(S), f.eks. via GitHub Pages eller lokalt med
`python -m http.server`. Den fungerer ikke åpnet direkte som fil.
