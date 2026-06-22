# Knowledge graphs of KEGG pathways

Build and query Neo4j knowledge graphs from the KEGG database. Each entity and relationship carries `map_ids[]` metadata indicating which pathway(s) it belongs to, enabling cross-pathway analysis.

> This project is based on [pathwaykg](https://github.com/eascarrunz/pathwaykg) but migrated to Neo4j as the primary storage and query engine.

## Installation

```bash
git clone https://github.com/PeriririIan/pathwaykg-neo4j
cd pathwaykg-neo4j
uv sync
```

## Neo4j setup

Start a Neo4j container for local development:

```bash
docker compose up -d
```

Configure connection in `.env`:

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_password
NEO4J_DATABASE=neo4j
```

## Building knowledge graphs

### Single pathway import

```bash
uv run kgbuild neo4j import -p hsa00010
```

### Batch import all pathways for an organism

```bash
# List available pathways
uv run kgbatch list-pathways -o hsa

# Import all human pathways
uv run kgbatch batch -o hsa
```

## Data model

### Node types

| Type | Description | Properties |
|------|-------------|------------|
| **Enzyme** | EC enzyme | `kegg_id`, `ec`, `map_ids[]` |
| **Reaction** | Biochemical reaction | `kegg_id`, `definition`, `map_ids[]` |
| **Compound** | Metabolite | `kegg_id`, `map_ids[]` |

### Relationships

| Type | Direction | Description |
|------|-----------|-------------|
| `:catalyzedBy` | Enzyme → Reaction | Enzyme catalyzes reaction |
| `:hasSubstrate` | Reaction → Compound | Substrate of reaction |
| `:hasProduct` | Reaction → Compound | Product of reaction |

### Multi-pathway support

Each node has a `map_ids[]` array listing all pathways it appears in. This enables queries like:

```cypher
-- Find entities shared between two pathways
MATCH (n) WHERE 'hsa00010' IN n.map_ids AND 'hsa00020' IN n.map_ids

-- Get all entities in a pathway
MATCH (n) WHERE 'hsa00010' IN n.map_ids
```

## Query examples

```cypher
-- Count nodes in a pathway
MATCH (n) WHERE 'hsa00010' IN n.map_ids
RETURN labels(n)[0] as type, count(n) as count

-- Find all reactions catalyzed by an enzyme
MATCH (e:Enzyme {kegg_id: '1.1.1.1'})-[:catalyzedBy]->(r:Reaction)
RETURN r.definition

-- Find shared compounds between pathways
MATCH (c:Compound)
WHERE 'hsa00010' IN c.map_ids AND 'hsa00020' IN c.map_ids
RETURN c.kegg_id, c.map_ids
```

## Management commands

```bash
# Show Neo4j configuration
uv run kgbuild neo4j config --show

# Validate Neo4j connection
uv run kgbuild neo4j config --validate

# Clear database before import
uv run kgbuild neo4j import -p hsa00010 --clear
```
