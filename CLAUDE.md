# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

KEGG Graph Database - 基于Neo4j的KEGG通路知识图谱，将所有KEGG通路数据存储到Neo4j图数据库中，每个实体和关系都带有所属KEGG map的元数据。

项目由pathwaykg改造而来，原项目使用RDF存储，现改为Neo4j。

## Tech Stack

- **Python >= 3.12**
- **Neo4j** (图数据库)
- **BioPython** (KEGG数据获取)
- **py2neo** 或 **neo4j-python-driver** (Neo4j连接)
- **pyvis** (可视化，可选)
- **uv** (包管理)

## Common Commands

```bash
# 安装依赖
uv sync

# 构建单个通路图谱（示例）
uv run kgbuild -p hsa00010

# 构建并导入Neo4j
uv run kgbuild -p hsa00010 --neo4j

# 构建所有通路（需配置 organism_list）
uv run kgbuild --all

# 可视化（可选）
uv run visualize -i output.ttl -o output.html
```

## Data Model

### Node Types
| 类型 | 描述 | 属性 |
|------|------|------|
| Gene | 基因节点 | kegg_id, label, uniprot, organism |
| KOTerm | KEGG Orthology term | kegg_id, label, ec_numbers |
| Reaction | 生化反应 | kegg_id, definition, equation, organisms |
| Compound | 化合物 | kegg_id, label, formula |

### Relationship Types
| 关系 | 方向 | 描述 |
|------|------|------|
| `:CATALYZES` | Gene → Reaction | 基因催化反应 |
| `:HAS_ORTHOLOG` | Gene → KOTerm | 基因的Orthology分类 |
| `:HAS_EC` | (双向) | EC酶分类号关联 |
| `:HAS_SUBSTRATE` | Reaction → Compound | 反应底物 |
| `:HAS_PRODUCT` | Reaction → Compound | 反应产物 |

### Metadata on All Entities
每个节点和关系都带有 `map_id` 属性，标识其所属的KEGG通路（如 `hsa00010`）。

## Architecture

```
pathwaykg/
├── build.py          # 核心构建逻辑，KGML解析、RDF/Neo4j图构建
├── fetch.py          # KEGG REST API数据获取
├── namespaces.py     # URI/命名空间定义
├── visualize.py      # 交互式HTML可视化（pyvis）
├── neo4j_adapter.py  # Neo4j适配器，RDF转Neo4j导入
```

### Build Pipeline

```
KEGG REST API → KGML解析 → 实体关系提取 → RDF Graph → kgimport → Neo4j
                                                      ↓
                                                map_id metadata
```

### Neo4j Import Commands

```bash
# 构建通路为TTL文件
uv run kgbuild -p hsa00010 > hsa00010.ttl

# 导入TTL到Neo4j
uv run kgimport -i hsa00010.ttl -m hsa00010

# 批量导入目录中所有TTL
uv run kgimport -i ./pathways/

# 清空数据库后导入
uv run kgimport -i hsa00010.ttl -m hsa00010 -c
```

## Key Files

- `build.py`: `build_kg()` 主函数，构建知识图谱
- `fetch.py`: `fetch_pathway_kgml()`, `fetch_gene_records()` 等数据获取
- `namespaces.py`: 定义KG、KEGG、EC等命名空间
- `neo4j_adapter.py`: 将RDF图导入Neo4j，支持批量导入和索引创建

## Development Notes

- KEGG通路entry格式：`{organism}{pathway_id}`，如 `hsa00010`（人类糖酵解）
- organism: 3字母如 `hsa`（human）, `eco`（E.coli）, `sce`（yeast）
- map/ko 开头的通路不支持（需要物种特异性数据）
- 每个实体/关系的 `map_id` 属性确保追踪来源通路