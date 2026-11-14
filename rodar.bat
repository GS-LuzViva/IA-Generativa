@echo off
chcp 65001 >nul
cd /d "%~dp0"
set VENV=%USERPROFILE%\venv-luzviva
set PY=%VENV%\Scripts\python.exe

rem cria o ambiente se ainda nao existir
if not exist "%PY%" (
    echo Criando o ambiente em %VENV% ...
    python -m venv "%VENV%" || (echo Python nao encontrado. Instale o Python 3.12 e marque "Add to PATH". & pause & exit /b)
    "%PY%" -m pip install --upgrade pip
    echo Instalando o PyTorch com suporte a GPU, pode demorar...
    "%PY%" -m pip install torch --index-url https://download.pytorch.org/whl/cu128
)

rem confere se as bibliotecas estao instaladas
"%PY%" -c "import torch, transformers, xgrammar, sentence_transformers, fastapi, pandas, ipykernel" 2>nul || (
    echo Instalando as bibliotecas do requirements.txt ...
    "%PY%" -m pip install -r requirements.txt
)

echo.
echo Carregando o assistente... o site abre sozinho em http://localhost:8001
echo Feche esta janela para parar.
start "" cmd /c "timeout /t 20 >nul & start http://localhost:8001"
"%PY%" -m uvicorn api:app --port 8001
pause
