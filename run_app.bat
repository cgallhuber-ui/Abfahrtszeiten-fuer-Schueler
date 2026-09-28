@echo off
setlocal

if not exist .venv\Scripts\python.exe (
    echo Erstelle virtuelle Umgebung...
    py -m venv .venv
)

call .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.headless true --server.port 8506

endlocal
