# -*- coding: utf-8 -*-
path = r"c:\Users\myerl\OneDrive\Desktop\Sclad\app.py"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()
lines[425] = '        print("База данных инициализирована.")\n'
with open(path, "w", encoding="utf-8") as f:
    f.writelines(lines)
print("OK")
