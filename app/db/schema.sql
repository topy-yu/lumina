CREATE TABLE IF NOT EXISTS photos (
  md5 TEXT PRIMARY KEY,
  relative_path TEXT NOT NULL,
  capture_time TEXT NULL,
  tags TEXT NOT NULL DEFAULT '[]'
);
