"""
The web Body: AURA rendered as a browser interface rather than a console.

Same contract as bodies/desktop — it publishes Percepts under its own
body id and consumes the Actions the Mind routes back to that id — but
the hardware it owns is the browser's: getUserMedia for the microphone,
speechSynthesis for the voice, an <img> for the camera preview.

It exists because the three things that have actually failed in this
project were all console hardware: a spawned child process that could not
read stdin (2026-10-06), pyttsx3's SAPI5 backend, and sounddevice. None of
those are in this path.
"""
