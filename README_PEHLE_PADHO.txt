EXE BANANE KA TARIKA (GitHub pe, ek hi baar, PC pe kuch install nahi karna)

1. github.com pe free account banao (ya login karo).
2. Upar "+" > "New repository" > naam: cutevoice > "Create repository".
3. "uploading an existing file" pe click karo. Zip EXTRACT karo, phir folder ke andar ki SAARI files
   drag karke daalo (app.py, audio_io.py, engine.py, winperf.py, selftest.py, requirements.txt,
   aur .github folder). Neeche "Commit changes" dabao.
4. Agar upar "Actions" tab mein "Build CuteVoiceStudio exe" nahi dikhe (.github folder upload nahi hua):
   "Add file" > "Create new file" > naam mein likho:  .github/workflows/build.yml
   aur build.yml ka poora text paste karke Commit karo.
5. "Actions" tab > "Build CuteVoiceStudio exe" > "Run workflow" (hara button).
   (Commit karte hi ye khud bhi chal jata hai.)
6. 3-5 minute baad us run pe click karo > neeche "Artifacts" mein "CuteVoiceStudio" download karo.
7. Zip extract karo > CuteVoiceStudio.exe pe double-click. Bas.

NAYA (v4 UI): window ab scroll hoti hai, Pro Controls right side mein khulte hain.
Agar run laal (fail) ho: us run pe click karo, laal step ka text copy karke bhej do.
PC pe khud banana ho to: BUILD_EXE.bat.
Windows Defender nayi unsigned exe pe warning de sakta hai ("More info" > "Run anyway").

AGAR ERROR AAYE:
- "Actions" tab mein kuch nahi dikhe  -> .github/workflows/build.yml upload nahi hui. Step 4 karo (build.yml paste).
- Run laal ho -> run kholo, laal (X wale) step pe click karo, uska aakhri 30 line ka text bhej do.
- "Selftest" step peela/laal ho par exe ban jaye to bhi chalega; exe ke saath selftest ka message bhej dena.
