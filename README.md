# Fanfarenzug-Neustrelitz-Seite

## Lokal starten

Voraussetzung ist Python 3.10 oder neuer. Lege im Projektordner eine lokale `.env`-Datei an:

```text
AUDIO_UPLOAD_PASSWORD=dein-passwort
```

Die `.env`-Datei wird nicht in Git eingecheckt. Alternativ kann `AUDIO_UPLOAD_PASSWORD` als Umgebungsvariable gesetzt werden. Anschließend den Server im Projektordner starten:

```sh
npm run dev
```

`AUDIO_UPLOAD_PASSWORD` legt das Passwort für Uploads und Löschen fest. Ohne gesetztes Passwort bleiben die Aufnahmen öffentlich abspielbar, aber Änderungen sind gesperrt. Upload- und Löschrechte werden zusätzlich serverseitig geprüft; die Anmeldung bleibt bis zu acht Stunden oder bis zum Abmelden gültig.

Die Seite ist anschließend unter `http://localhost:5173` erreichbar.

Für Video-Uploads muss außerdem FFmpeg installiert und über `PATH` erreichbar sein. Videos werden serverseitig in MP3 umgewandelt; auf der Seite erscheinen sie wie andere Audios.

Hochgeladene Audios liegen im Ordner `uploads/`; Titel, optionale Nummer (1–30), Register und Dateinamen werden in `data/audio.sqlite3` gespeichert. Das Register muss beim Upload aus den vorgegebenen Bläser- und Trommelstimmen gewählt werden. Nummerierte Aufnahmen werden aufsteigend sortiert; Aufnahmen ohne Nummer folgen alphabetisch. Beide Ordner werden beim ersten Start angelegt und sind von Git ausgeschlossen. Für Backups müssen beide Ordner gesichert werden.

Der Server ist für die lokale Entwicklung gedacht und auf 100 MB pro Anfrage begrenzt. Für einen öffentlichen Betrieb sind HTTPS, ein starkes eigenes Passwort und ein dauerhafter, gesicherter Speicherort für Datenbank und Audiodateien erforderlich.