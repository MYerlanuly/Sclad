# -*- coding: utf-8 -*-
path = "app.py"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()
lines[120] = '        print("Выполняется миграция таблицы equipment: изменение UNIQUE constraint на (name, serial_number)...")\n'
lines[173] = '        print("Миграция выполнена успешно: UNIQUE constraint изменён на (name, serial_number).")\n'
with open(path, "w", encoding="utf-8") as f:
    f.writelines(lines)
print("OK")
