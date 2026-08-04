@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  Discord 오버레이 번역기 - exe 빌드
echo ============================================================
echo.
echo [1/2] 필요한 패키지 확인/설치 중...
python -m pip install --quiet --upgrade pyinstaller
python -m pip install --quiet -r requirements.txt

echo [2/2] exe 빌드 중... (몇 분 걸릴 수 있습니다)
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name DiscordTranslator ^
  --collect-submodules comtypes ^
  --collect-submodules pywinauto ^
  discord_screen_overlay.py

echo.
if exist "dist\DiscordTranslator.exe" (
  echo ============================================================
  echo  완료!  dist\DiscordTranslator.exe 를 더블클릭해서 실행하세요.
  echo  종료는 우하단 트레이 아이콘 우클릭 ^> 종료
  echo ============================================================
) else (
  echo [오류] 빌드에 실패했습니다. 위의 메시지를 확인하세요.
)
echo.
pause
