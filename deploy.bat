@echo off
chcp 65001 >nul
cd /d "%~dp0"

set MSG=%~1
if "%MSG%"=="" set MSG=Update Sticker Slicer Bot

echo [1/3] Staging files in Git...
git add bot.py slicer.py db.py requirements.txt .env.example .gitignore .gitattributes setup_vm.sh enable_autoupdate.sh deploy.bat README.md INSTRUCTION.md

echo [2/3] Creating commit: "%MSG%"...
git commit -m "%MSG%"

echo [3/3] Pushing changes to GitHub (origin/main)...
git push origin main

echo.
echo Done! Changes pushed to GitHub.
echo Your VM will automatically pull the update and restart the bot within 30 seconds.
pause
