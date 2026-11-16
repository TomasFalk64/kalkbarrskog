# Artfynd i projektet Upplands kalkbarrskogar

En lokal Streamlit-applikation för att hämta och sammanställa artfynd från Artportalen (via SLU Artdatabankens SOS-API) inom fördefinierade polygoner.

## Funktioner
* **Områdesbaserad sökning:** Läser in polygoner från mappen `data/` (stödjer `.zip`, `.shp`, `.geojson` och `.gpkg`) och gör separata anrop per område.
* **Arturval:** Möjlighet att hämta antingen *samtliga arter* eller enbart *naturvårdsarter* (rödlistade, signalarter samt utvalda taxa).
* **Tidsfiltrering:** Filtrering på årsintervall (från år – till år) baserat på observationens startdatum[cite: 2].
* **Statistik:** Sammanställer observationer, unika arter och lokaler per valt område.

## Installation (första gången)

1. Skapa en virtuell miljö och installera beroenden:
   ```bash
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt


Lägg dina polygonfiler i mappen data/.

Konfiguration av API-nyckel
Applikationen kräver en prenumerationsnyckel till SLU Artdatabankens Species Observation System (SOS). Nyckeln kan anges på ett av följande sätt:

Skapa en .env-fil i projektets rotmapp och lägg till:    ARTPORTALEN_API_KEY=din_api_nyckel_här
Eller ange i applikationen

## Starta applikationen
Kör följande kommando i terminalen:

Bash
.\.venv\Scripts\python.exe -m streamlit run main.py
Applikationen öppnas automatiskt i din webbläsare (vanligtvis på http://localhost:8501).

## Data
Du kan välja bland polygoner som ligger i mappen data

## Urval
Du väljer vilja områden som ska hämtas utifrån de som ligger i mappen data
Möjlighet att välja alla observationer eller Rödlistade + skogsstyrelsenssignalarter+ utvalda taxonID
Möjlighet att välja vilka år

## Export
Du kan spara hämtade artfynd till csv-fil

## Begränsningar
Sökningen hämtar endast offentliga observationer i Artportalen; skyddade fynd ingår inte.