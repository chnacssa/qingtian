-- 汇川 v2.4 Phase 5: 知识图谱 — 交叉引用网络
-- knowledge_links 表 + 索引
-- 回滚: DROP TABLE IF EXISTS huichuan.knowledge_links;

CREATE TABLE IF NOT EXISTS huichuan.knowledge_links (
    link_id      BIGSERIAL PRIMARY KEY,
    source_id    UUID NOT NULL REFERENCES huichuan.knowledge_entries(knowledge_id) ON DELETE CASCADE,
    target_id    UUID NOT NULL REFERENCES huichuan.knowledge_entries(knowledge_id) ON DELETE CASCADE,
    link_type    TEXT NOT NULL CHECK (link_type IN ('related','contradicts','extends','depends','cites')),
    confidence   FLOAT DEFAULT 1.0,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, target_id, link_type)
);

CREATE INDEX IF NOT EXISTS idx_kl_source ON huichuan.knowledge_links(source_id);
CREATE INDEX IF NOT EXISTS idx_kl_target ON huichuan.knowledge_links(target_id);
