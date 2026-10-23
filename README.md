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

## Laufzeit-Abhängigkeiten

Benötigt:

- `ifuse`
- `libimobiledevice` (u. a. `idevice_id`, `idevicepair`, `ideviceinfo`)
- `exiftool`
- GTK4 + libadwaita + PyGObject (Systempakete)

Optional:

- `gstreamer1.0-libav` (integrierte Wiedergabe von iPhone-Videos in der Vorschau)

```bash
sudo apt install gstreamer1.0-libav
```

## Mediathek-Struktur

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

## Hinweise zu FAT32 / vfat

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
