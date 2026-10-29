# iPhone Fotos (Linux/GTK)

Moderne Import-App für iPhone-Fotos und -Videos unter Linux, angelehnt an den Import-Workflow von Apple Fotos.

## Funktionen

- Gerätebereich wie in Apple Fotos: iPhone erscheint in der Seitenleiste.
- Import-Ansicht mit:
  - **Alle neuen Objekte importieren**
  - **Auswahl importieren**
  - Album-Ziel (`Ohne`, bestehendes Album, `Neues Album …`)
  - Bereich **Neue Objekte** und **Bereits importiert**
  - laufender Fortschritt + **Importieren stoppen**
- Mediathek mit Modi **Jahre / Monate / Tage / Alle Fotos**
- Bereiche für **Videos**, **Live Photos**, **RAW**, **Bildschirmfotos**, **Importe**, Alben
- Bereich **Ausgeblendet** für bereits lokal ausgeblendete Objekte; kein automatischer
  Abgleich des iPhone-Ausblendestatus
- Vorschau-Dialog für Fotos, Live Photos und Videos
- Hintergrundmodus („Beim Anschließen öffnen“) wie „Fotos öffnen“ auf macOS
- Wartung:
  - bestehende Dateien erfassen (indexieren)
  - alte DCIM-Struktur in Datumsstruktur einsortieren
  - Mediathek an einen anderen Ort umziehen

## Installation

Im Projektordner:

```bash
./install.sh
```

Das Skript:

- erstellt/aktualisiert die venv unter `~/.local/share/ios-photos/venv`
- installiert das Paket per `pip install .`
- legt den Starter `~/.local/bin/ios-photos` an
- installiert `.desktop`-Dateien und Icons unter `~/.local/share`

Das App-Icon ist eine eigenstaendige, sechsfarbige Rosette auf hellem Hintergrund.
Nach einem Icon-Update die App vollstaendig beenden und neu starten, damit auch
das Dock das neue Symbol uebernimmt.

Danach starten mit:

```bash
ios-photos
```

## Automatisch beim Anschließen öffnen

Beim ersten Start aktiviert die App den Hintergrundmonitor. Er bleibt nach dem
Schließen des Fensters aktiv und startet bei der nächsten Anmeldung automatisch.
Ein angeschlossenes iPhone öffnet das Fenster, sobald es erkannt wird.
Falls nötig, zeigt die App Hinweise zum Entsperren oder Freigeben des Geräts.
Die Geräteansicht prüft den Sperrzustand regelmäßig und zeigt nur bei entsperrtem
iPhone Medien an, auch wenn iOS dem vertrauten Rechner weiterhin Dateizugriff erlaubt.
Bei einer gemeldeten Sperre wird die Geräteansicht geleert; ein laufender Import
kopiert weiter, solange iOS den Dateizugriff erlaubt. Verweigert iOS das Öffnen
oder Lesen einer Quelldatei (Zugriffs-/Eingabefehler), zeigt die App einen
Wartehinweis und versucht die aktuelle Datei jede Sekunde erneut. Nach erneutem
Zugriff wird sie vollständig neu kopiert; bereits fertige Dateien bleiben erhalten.
**Importieren stoppen** beendet auch diesen Wartezustand. Andere Gerätefehler
oder das Trennen des iPhones beenden den Import weiterhin.
Bereits lokal importierte Dateien bleiben unabhängig davon in der Mediathek verfügbar.
Bei vorzeitigem Leseende oder geänderter Quellgröße versucht der Geräteimport
die aktuelle Datei höchstens dreimal vollständig neu. Bleibt die Größenprüfung
erfolglos, wird die Datei nicht übernommen; die Fehlermeldung nennt kopierte
und erwartete Bytezahlen. Ziellaufwerksfehler werden nicht als Gerätesperre wiederholt.

Miniaturanfragen derselben Datei werden auch während der Verarbeitung gebündelt.
Vorübergehende Vorschaufehler werden bei einer erneuten Anfrage nach fünf Sekunden
wieder versucht; veraltete Ergebnisse dürfen keine inzwischen neu belegte Kachel ändern.

Unter **Einstellungen → iPhone anschließen → Im Hintergrund auf iPhones warten**
lässt sich das dauerhaft abschalten. **Beim Anschließen öffnen** steuert das
Verhalten zusätzlich pro Gerät. **Strg+Q** beendet auch den Hintergrundmonitor;
bis zum nächsten App-Start oder Anmelden erfolgt dann kein automatisches Öffnen.

## Laufzeit-Abhängigkeiten

Benötigt:

- `ifuse`
- `libimobiledevice` (u. a. `idevice_id`, `idevicepair`, `ideviceinfo`)
- `exiftool`
- GTK4 + libadwaita + PyGObject (Systempakete)
- GVfs-Papierkorbdienst (`gvfs-daemons`, unter GNOME normalerweise vorhanden)

Optional:

- `gstreamer1.0-libav` (integrierte Wiedergabe von iPhone-Videos in der Vorschau)

```bash
sudo apt install gstreamer1.0-libav
```

## Mediathek-Struktur

### Direkter Import ohne iPhone-Fotodatenbank

Die App liest ausschließlich die zugänglichen Mediendateien unter `DCIM`.
Sie öffnet weder `PhotoData/Photos.sqlite` noch deren WAL-/SHM-Dateien und
erstellt kein Gerätebackup. Eine automatische Erkennung oder ein separater
Bereich für auf dem iPhone gelöschte Objekte ist nicht vorhanden.

Vor dem Import unerwünschte Objekte auf dem iPhone selbst verwalten. Die
Geräteansicht aktualisiert sich automatisch im Hintergrund, sobald du zur
Geräteansicht oder zum App-Fenster zurückkehrst, sowie nach fehlgeschlagenen
Übertragungen; Position und Auswahl bleiben dabei erhalten. Solange die Ansicht
ununterbrochen offen bleibt, hilft **Neu einlesen**. Ein Verschieben nach
**Zuletzt gelöscht** garantiert nicht, dass iOS die Dateien nicht mehr über USB
anbietet. Alle weiterhin zugänglichen DCIM-Dateien können importiert werden.
Die App löscht nichts auf dem Gerät und verlangt kein endgültiges Löschen.

Wurde eine Datei nach dem Einlesen auf dem iPhone gelöscht, wird sie beim Import
als **„nicht mehr auf dem Gerät“** übersprungen – ohne den übrigen Import
abzubrechen – und die Ansicht aktualisiert sich anschließend.

Auch der iPhone-Ausblendestatus wird nicht mehr automatisch ermittelt.
Neu importierte zugängliche Dateien können deshalb in der normalen Übersicht
erscheinen, selbst wenn sie auf dem iPhone ausgeblendet sind. Bereits lokal
gespeicherte Ausblendungen bleiben erhalten. Frühere experimentelle Löschflags
werden ignoriert; keine lokalen Originale oder Alben werden entfernt.

### Fotos und Videos entfernen

In der lokalen Mediathek ein oder mehrere Objekte auswählen und in der
Auswahlleiste **In den Papierkorb …** anklicken (auch im Rechtsklick-Menü).
**Alle auswählen** steht direkt in der Kopfzeile; **Strg+A** funktioniert auch
ohne vorherigen Bildklick. Beides wählt die Objekte der aktuellen Ansicht,
auch in der Importansicht. In der Jahres-/Monatsübersicht wechselt die Auswahl
zur Ansicht **Alle Fotos**.
Einzelne Klicks wählen weitere Objekte aus; ein erneuter Klick wählt das jeweilige
Objekt wieder ab. **Umschalt+Klick** wählt einen zusammenhängenden Bereich,
**Strg+Umschalt+Klick** ergänzt diesen zur Auswahl. **Esc** hebt die Auswahl auf.
Der Fortschrittsdialog zeigt die bearbeiteten Dateien und den aktuellen Dateipfad.
Ein Objekt kann mehrere Dateien umfassen. Der Index wird alle 50 verschobenen
Dateien gespeichert; auf langsamen Laufwerken kann ein Dateivorgang dauern.
**Abbrechen** stoppt nach dem aktuellen Objekt, nicht mitten in einem laufenden
Dateisystemzugriff. Bereits verschobene Dateien bleiben im Papierkorb.
Beim Beenden während eines Dateivorgangs bietet die App **Stoppen und schließen**
an und wartet vor dem Schließen auf die aktuelle Datei.
Nach Bestätigung werden alle zugehörigen Dateien einschließlich Live-Photo-Videos,
RAW-Dateien und Begleitdateien in den System-Papierkorb verschoben. Die Objekte
verschwinden aus allen lokalen Alben; Originale auf dem iPhone bleiben unverändert.
**Aus Album entfernen** entfernt weiterhin nur die Album-Zuordnung.

Bei mehreren Dateien legt die App einmalig einen Sammelordner **iPhone Fotos - …**
im System-Papierkorb desselben Laufwerks an und verschiebt die Dateien direkt
dorthin, unter Beibehaltung ihrer Unterordnerstruktur. Das vermeidet die teure
Papierkorb-Verwaltung pro Datei auf FAT-Laufwerken; die Dateien werden nicht kopiert.
Die Ermittlung des Papierkorb-Ordners benötigt den GNOME-Papierkorbdienst (GVfs).
Ist er nicht erreichbar, bleiben die Originaldateien unverändert und die App
meldet einen Fehler. Einzeldateien werden weiterhin einzeln in den Papierkorb verschoben.
Zum Wiederherstellen den Sammelordner im Dateimanager öffnen und die gewünschten
Dateien in ihre bisherigen Unterordner der Mediathek zurückverschieben.

Es gibt keinen automatischen Rückfall auf endgültiges Löschen. Unterstützt das
Laufwerk keinen Papierkorb, meldet die App einen Fehler und behält betroffene
Dateien. Bei Teilerfolgen liegen bereits verschobene Dateien im Papierkorb,
die übrigen bleiben im Index. Wiederherstellen erfolgt über den Dateimanager;
danach gegebenenfalls **Vorhandene Fotos erfassen** verwenden. Album-Zuordnungen
werden dabei nicht automatisch wiederhergestellt. Entfernte Objekte können vom
iPhone erneut importiert werden.

Originaldateien werden unverändert unter dem Mediathek-Pfad gespeichert:

```text
<Mediathek>/
  2026/
    10/
      16/
        IMG_1234.HEIC
        IMG_1234.MOV
  .ios-photos/
    library.db
```

- Ablage nach `JJJJ/MM/TT`
- bei Namenskollisionen wird `_1`, `_2`, … angehängt
- Alben sind virtuelle Zuordnungen in der Datenbank (keine Symlinks)
- Lokal ausgeblendete Objekte bleiben erhalten und sind nur im Bereich **Ausgeblendet** sichtbar

## Hinweise zu FAT32 / vfat

- Beim Import verwendet der **lokale Mediathek-Index** auf FAT/vfat SQLite-WAL
  mit `synchronous=FULL`. Das vermeidet die langsame Neuanlage des
  Rollback-Journals nach jedem Objekt. Jedes Objekt wird weiterhin einzeln
  committed; das `fsync` der kopierten Dateien bleibt erhalten.
- Während die Mediathek geöffnet ist, gehören `library.db-wal` und
  `library.db-shm` zum Index. Diese Dateien nicht einzeln löschen oder
  `library.db` im laufenden Betrieb allein kopieren. Vor einer manuellen
  Sicherung die App vollständig beenden und den gesamten Mediathek-Ordner sichern.
  Der integrierte Umzug schließt die Mediathek vor dem Kopieren.
- Diese Optimierung greift nicht auf die iPhone-Fotodatenbank zu und aktiviert
  WAL nicht für andere Dateisysteme wie Netzwerkfreigaben.
- Dateinamen sind nicht case-sensitiv → Kollisionen werden entsprechend behandelt
- maximale Dateigröße: **4 GB** (Import größerer Dateien wird verhindert)
- Zeitstempelauflösung kann gröber sein (typisch 2 Sekunden)
- Python-venv im Projektordner ist auf vfat oft problematisch (Symlinks) – daher liegt die venv standardmäßig unter `~/.local/share/ios-photos/venv`

## Altbestand übernehmen

Wenn im Mediathek-Ordner bereits Fotos liegen:

1. In der Mediathek auf **Erfassen** klicken (Dateien in den Index aufnehmen)
2. Optional **In Datumsordner einsortieren**, um alte `100APPLE`-Strukturen nach `JJJJ/MM/TT` zu verschieben

## Entwicklung / Tests

Tests ausführen:

```bash
PYTHONPATH=.:tests ~/.local/share/ios-photos/venv/bin/python -m pytest -q
```
