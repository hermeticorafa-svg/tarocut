@echo off
rem Atalho de duplo clique para a janela do tarocut.
rem pythonw = sem janela preta de console atras.
cd /d "%~dp0"
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0gui.py"
