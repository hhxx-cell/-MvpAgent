PRAGMA foreign_keys = ON;

CREATE TABLE products (
    sku_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0)
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    sku_id TEXT NOT NULL REFERENCES products(sku_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    status INTEGER NOT NULL CHECK (status BETWEEN 0 AND 7),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency = 'CNY')
);

CREATE TABLE logistics (
    logistics_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
    carrier TEXT NOT NULL,
    tracking_no TEXT NOT NULL UNIQUE,
    status INTEGER NOT NULL CHECK (status BETWEEN 0 AND 3),
    updated_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE INDEX idx_orders_owner_created ON orders(tenant_id, user_id, created_at);
CREATE INDEX idx_orders_sku ON orders(sku_id);
CREATE INDEX idx_logistics_order ON logistics(order_id);
