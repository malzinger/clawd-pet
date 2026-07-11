# Sprites

Die animierten GIFs in diesem Ordner stammen aus dem MIT-lizenzierten
Community-Projekt [KebeliSamet0/clawd](https://github.com/KebeliSamet0/clawd)
(pixelgenaue Nachbildung des Claude-Code-Maskottchens).

Der komplette Satz (16 GIFs) liegt hier. Genutzt werden aktuell:

| Datei                        | Zustand in der App                     |
| ---------------------------- | -------------------------------------- |
| `clawd-sleeping.gif`         | keine Aktivität                        |
| `clawd-idle.gif`             | Budget übrig, keine Aktivität          |
| `clawd-building.gif`         | Befehle ausführen / 50–80 % Auslastung |
| `clawd-typing.gif`           | Dateien bearbeiten/schreiben           |
| `clawd-idle-reading.gif`     | lesen / suchen / surfen                |
| `clawd-thinking.gif`         | arbeiten ohne Tool (Claude denkt)      |
| `clawd-notification.gif`     | Claude wartet auf deine Eingabe        |
| `clawd-happy.gif`            | Turn fertig                            |
| `clawd-debugger.gif`         | 80–100 % / Tool-Fehler                 |
| `clawd-error.gif`            | über dem Limit                         |
| `clawd-react-double-jump.gif`| Streichel-Reaktion (Doppelklick)       |
| `clawd-react-annoyed.gif`    | genervt (zu oft gestreichelt)          |
| `clawd-juggling.gif`         | zufällige Idle-Animation (jongliert)   |
| `clawd-conducting.gif`       | zufällige Idle-Animation (dirigiert)   |
| `clawd-sweeping.gif`         | zufällige Idle-Animation (fegt)        |
| `clawd-carrying.gif`         | zufällige Idle-Animation (trägt etwas) |

Alle 16 sind damit in Verwendung. Die Idle-Flourishes (juggling, conducting,
sweeping, carrying) spielt Clawd ab und zu zufällig, solange er entspannt
herumsteht (siehe `IDLE_FLOURISHES` in `clawd_pet.py`).

Fehlen die Dateien, zeichnet die App automatisch einen eingebauten
Vektor-Clawd als Fallback (mit sinnvollem Rückfall auf Basis-Moods).
