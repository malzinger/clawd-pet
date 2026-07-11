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

Die übrigen (carrying, conducting, juggling, sweeping, react-annoyed) liegen
für spätere Verwendung bereit — einfach in `SPRITE_FILES` in `clawd_pet.py`
zuordnen.

Fehlen die Dateien, zeichnet die App automatisch einen eingebauten
Vektor-Clawd als Fallback (mit sinnvollem Rückfall auf Basis-Moods).
