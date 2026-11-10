-- SQLite schema for inventory system

PRAGMA foreign_keys = ON;

-- =========================
-- 1. Equipment and statuses
-- =========================

CREATE TABLE IF NOT EXISTS equipment_statuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    serial_number TEXT NOT NULL,
    description TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    status_id INTEGER NOT NULL,
    equipment_kind TEXT NOT NULL DEFAULT 'other',
    FOREIGN KEY (status_id) REFERENCES equipment_statuses(id),
    UNIQUE(name, serial_number)
);

-- =========================
-- 2. Machines, types, models
-- =========================

CREATE TABLE IF NOT EXISTS machine_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS machine_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    machine_type_id INTEGER NOT NULL,
    FOREIGN KEY (machine_type_id) REFERENCES machine_types(id)
);

CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inventory_number TEXT NOT NULL UNIQUE,
    machine_model_id INTEGER NOT NULL,
    FOREIGN KEY (machine_model_id) REFERENCES machine_models(id)
);

-- =========================
-- 3. SIM cards and statuses
-- =========================

CREATE TABLE IF NOT EXISTS sim_statuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS sim_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_number TEXT NOT NULL,
    iccid TEXT NOT NULL UNIQUE,
    ip_address TEXT,
    status_id INTEGER NOT NULL,
    FOREIGN KEY (status_id) REFERENCES sim_statuses(id)
);

-- Relation SIM card <-> machine (one-to-one both ways)
CREATE TABLE IF NOT EXISTS machine_sim_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL,
    sim_card_id INTEGER NOT NULL,
    installed_at TEXT NOT NULL DEFAULT (datetime('now')),
    removed_at TEXT,
    FOREIGN KEY (machine_id) REFERENCES machines(id),
    FOREIGN KEY (sim_card_id) REFERENCES sim_cards(id)
);

-- Ensure one active SIM per machine and one machine per SIM (partial unique indexes)
CREATE UNIQUE INDEX IF NOT EXISTS idx_machine_sim_cards_active_machine
    ON machine_sim_cards (machine_id)
    WHERE removed_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_machine_sim_cards_active_sim
    ON machine_sim_cards (sim_card_id)
    WHERE removed_at IS NULL;

-- =========================
-- 4. Позиции для датчиков (при установке оборудования вида «датчик»)
-- =========================

CREATE TABLE IF NOT EXISTS sensor_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    position_group TEXT NOT NULL DEFAULT 'wheel',
    label TEXT,
    sort_order INTEGER
);

-- =========================
-- 5. Equipment installation on machines
-- =========================

CREATE TABLE IF NOT EXISTS machine_equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL,
    equipment_id INTEGER NOT NULL,
    installed_at TEXT NOT NULL DEFAULT (datetime('now')),
    removed_at TEXT,
    sensor_position_id INTEGER,
    FOREIGN KEY (machine_id) REFERENCES machines(id),
    FOREIGN KEY (equipment_id) REFERENCES equipment(id),
    FOREIGN KEY (sensor_position_id) REFERENCES sensor_positions(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_machine_equipment_sensor_position
    ON machine_equipment (machine_id, sensor_position_id)
    WHERE sensor_position_id IS NOT NULL AND removed_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_machine_equipment_active_equipment
    ON machine_equipment (equipment_id)
    WHERE removed_at IS NULL;

-- =========================
-- 6. Operations log
-- =========================

CREATE TABLE IF NOT EXISTS operations_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    operation_type TEXT NOT NULL, -- install / remove / status_change
    entity_type TEXT NOT NULL,    -- equipment / sim_card / sensor / machine
    entity_id INTEGER NOT NULL,
    description TEXT
);

-- =========================
-- 6.1 Техобслуживание (ТО) техники — раз в 3 месяца
-- =========================

CREATE TABLE IF NOT EXISTS machine_maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL,
    performed_at TEXT NOT NULL,       -- дата проведения ТО (YYYY-MM-DD)
    next_due_at TEXT NOT NULL,        -- следующее ТО (performed_at + 3 мес)
    performed_by TEXT,               -- ФИО инженеров
    remarks TEXT,                     -- замечания
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (machine_id) REFERENCES machines(id)
);

-- =========================
-- 7. Reference data
-- =========================

-- Equipment statuses
INSERT OR IGNORE INTO equipment_statuses (id, name) VALUES
    (1, 'На складе'),
    (2, 'Установлено'),
    (3, 'Демонтировано'),
    (4, 'Неисправно'),
    (5, 'Списано');

-- SIM card statuses
INSERT OR IGNORE INTO sim_statuses (id, name) VALUES
    (1, 'На складе'),
    (2, 'Установлена'),
    (3, 'Заблокирована'),
    (4, 'Списана');

-- Sensor positions (для оборудования вида «датчик»): wheel = колёса (датчики давления шин), general = прочие (топливо, скорость и т.д.)
-- Колёса: ПЛ, ПП, ЗЛН, ЗЛВ, ЗПН, ЗПВ
INSERT OR IGNORE INTO sensor_positions (id, code, position_group, label, sort_order) VALUES
    (1, 'ПЛ', 'wheel', 'Передний левый', 1),
    (2, 'ПП', 'wheel', 'Передний правый', 2),
    (3, 'ЗЛН', 'wheel', 'Задний левый наружный', 3),
    (4, 'ЗЛВ', 'wheel', 'Задний левый внутренний', 4),
    (5, 'ЗПН', 'wheel', 'Задний правый наружный', 5),
    (6, 'ЗПВ', 'wheel', 'Задний правый внутренний', 6),
    (7, 'Топливный бак', 'general', NULL, NULL),
    (8, 'Датчик скорости', 'general', NULL, NULL),
    (9, 'Общий', 'general', NULL, NULL);

-- Тестовые данные (техника, оборудование, SIM) убраны — при пересоздании БД она будет пустой.
-- Справочники выше (статусы, позиции датчиков) нужны для работы. Пользователя создайте: flask create-user -u admin -p ваш_пароль -r admin
