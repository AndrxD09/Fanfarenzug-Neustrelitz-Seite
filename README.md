# Fanfarenzug-Neustrelitz-Seite

## Lokal starten

Voraussetzung ist Python 3.10 oder neuer. Im Projektordner starten:

```sh
npm run dev
```

Die Seite ist anschließend unter `http://localhost:5173` erreichbar.

Hochgeladene Audios liegen im Ordner `uploads/`; Titel und Dateinamen werden in `data/audio.sqlite3` gespeichert. Beide Ordner werden beim ersten Start angelegt und sind von Git ausgeschlossen. Für Backups müssen beide Ordner gesichert werden.

Der Upload ist derzeit für die lokale Entwicklung gedacht und auf 100 MB pro Anfrage begrenzt. Vor einem öffentlichen Betrieb sollte ein Login ergänzt und ein dauerhafter, gesicherter Speicherort für Datenbank und Audiodateien eingerichtet werden.