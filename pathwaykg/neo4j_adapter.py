#!/usr/bin/env python3

"""Neo4j adapter for KEGG pathway knowledge graphs"""

from dataclasses import dataclass
from typing import Optional
from pathlib import Path
from tqdm import tqdm
from neo4j import GraphDatabase


@dataclass
class Neo4jConfig:
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = "password"
    database: str = "neo4j"


class Neo4jAdapter:
    def __init__(self, config: Optional[Neo4jConfig] = None):
        self.config = config or Neo4jConfig()
        self.driver = None
        self._node_counter = 0
        self._rel_counter = 0

    def connect(self) -> None:
        self.driver = GraphDatabase.driver(
            self.config.uri,
            auth=(self.config.user, self.config.password)
        )

    def close(self) -> None:
        if self.driver:
            self.driver.close()

    def clear_database(self) -> None:
        with self.driver.session(database=self.config.database) as session:
            session.run("MATCH (n) DETACH DELETE n")
        self._node_counter = 0
        self._rel_counter = 0

    def create_indexes(self) -> None:
        with self.driver.session(database=self.config.database) as session:
            # Kegg_id as unique constraint for each node label
            for label in ["Enzyme", "Reaction", "Compound"]:
                session.run(f"CREATE CONSTRAINT kegg_id_{label.lower()} IF NOT EXISTS FOR (n:{label}) REQUIRE n.kegg_id IS UNIQUE")
            # EC index for Enzyme
            session.run("CREATE INDEX enzyme_ec IF NOT EXISTS FOR (e:Enzyme) ON (e.ec)")

    def import_from_ttl(self, ttl_path: Path, map_id: str) -> tuple[int, int]:
        """Import RDF Turtle file into Neo4j with map_id metadata"""
        from rdflib import Graph, RDF

        graph = Graph()
        graph.parse(str(ttl_path), format="turtle")

        # Build node mapping: kegg_id -> entity_id
        node_map = {}  # kegg_id -> entity_id

        with self.driver.session(database=self.config.database) as session:
            # First pass: create all nodes
            for subject in tqdm(graph.subjects(), desc="Importing nodes"):
                node_type = self._get_node_type(subject, graph)
                if not node_type:
                    continue

                kegg_id = self._extract_kegg_id(str(subject), node_type)
                self._node_counter += 1
                entity_id = self._node_counter
                node_map[kegg_id] = entity_id

                props = self._get_node_properties(subject, graph, kegg_id, node_type)

                label = self._get_label(node_type)
                # Use kegg_id as merge key so same entity across maps becomes one node
                # Append map_id to array if not already present
                query = f"""
                    MERGE (n:{label} {{kegg_id: $kegg_id}})
                    SET n.map_ids = CASE
                        WHEN n.map_ids IS NULL THEN [$map_id]
                        WHEN NOT $map_id IN n.map_ids THEN n.map_ids + [$map_id]
                        ELSE n.map_ids
                    END,
                    n.kegg_id = $kegg_id,
                    n.entity_id = $entity_id,
                    n.name = $name
                """
                params = {
                    "kegg_id": kegg_id,
                    "entity_id": entity_id,
                    "map_id": map_id,
                    "name": str(entity_id)
                }
                session.run(query, params)
                node_map[kegg_id] = entity_id

            # Second pass: create relationships
            for s, p, o in tqdm(graph, desc="Importing relationships"):
                p_name = str(p).split("/")[-1]
                if p_name in ("hasSubstrate", "hasProduct", "catalyzedBy"):
                    s_label = self._get_label(self._get_node_type(s, graph))
                    o_label = self._get_label(self._get_node_type(o, graph))

                    if not s_label or not o_label:
                        continue

                    s_kegg = self._extract_kegg_id(str(s), s_label)
                    o_kegg = self._extract_kegg_id(str(o), o_label)

                    if s_kegg not in node_map or o_kegg not in node_map:
                        continue

                    self._rel_counter += 1
                    edge_id = self._rel_counter

                    query = f"""
                        MATCH (a:{s_label} {{kegg_id: $source_kegg}})
                        MATCH (b:{o_label} {{kegg_id: $target_kegg}})
                        MERGE (a)-[r:{p_name}]->(b)
                        SET r.map_ids = CASE
                            WHEN r.map_ids IS NULL THEN [$map_id]
                            WHEN NOT $map_id IN r.map_ids THEN r.map_ids + [$map_id]
                            ELSE r.map_ids
                        END
                    """
                    params = {
                        "source_kegg": s_kegg,
                        "target_kegg": o_kegg,
                        "map_id": map_id
                    }
                    session.run(query, params)

            nodes_created = self._node_counter
            rels_created = self._rel_counter

        return nodes_created, rels_created

    def _get_node_type(self, uri, graph) -> Optional[str]:
        from rdflib import RDF
        for obj in graph.objects(uri, RDF.type):
            type_str = str(obj).split("/")[-1]
            if type_str in ("Enzyme", "Reaction", "Compound"):
                return type_str
        return None

    def _get_label(self, node_type: str) -> str:
        mapping = {
            "Enzyme": "Enzyme",
            "Reaction": "Reaction",
            "Compound": "Compound"
        }
        return mapping.get(node_type, "Node")

    def _extract_kegg_id(self, uri: str, node_type: str) -> str:
        """Extract KEGG ID from URI"""
        if "kegg.jp/entry/" in uri:
            return uri.split("/")[-1]
        if "/organism/" in uri:
            parts = uri.split("/")
            return f"{parts[-2]}:{parts[-1]}"
        if "/entry/K" in uri:
            return "ko:" + uri.split("/")[-1]
        if "/entry/" in uri:
            # EC numbers: extract the EC code
            ec_code = uri.split("/")[-1]
            if ec_code.startswith("EC"):
                return ec_code  # e.g., "EC:1.1.1.1"
            return "EC:" + ec_code
        return uri.split("/")[-1]

    def _get_node_properties(self, uri, graph, kegg_id: str, node_type: str) -> dict:
        from rdflib import RDFS, OWL
        props = {}
        props["uri"] = str(uri)

        for p, o in graph.predicate_objects(uri):
            p_name = str(p).split("/")[-1]

            if p_name == "label" and isinstance(o, str):
                props["description"] = o
            elif p_name == "keggID":
                props["kegg_url"] = str(o)
            elif p_name == "sameAs" and "uniprot" in str(o).lower():
                props["uniprot"] = str(o).split("/")[-1]

        # For Enzyme nodes, store EC as dedicated property
        if node_type == "Enzyme" and kegg_id.startswith("EC:"):
            props["ec"] = kegg_id

        return props

    def batch_import(self, ttl_dir: Path, organism: str) -> dict:
        """Batch import all TTL files for an organism"""
        ttl_files = list(Path(ttl_dir).glob(f"{organism}*.ttl"))
        total_nodes = 0
        total_rels = 0

        for ttl_file in tqdm(ttl_files, desc=f"Importing {organism} pathways"):
            map_id = ttl_file.stem
            nodes, rels = self.import_from_ttl(ttl_file, map_id)
            total_nodes += nodes
            total_rels += rels

        return {"files": len(ttl_files), "nodes": total_nodes, "relationships": total_rels}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Import KEGG pathway TTL into Neo4j")
    parser.add_argument("-i", "--input", type=Path, required=True, help="TTL file or directory")
    parser.add_argument("-m", "--map-id", type=str, help="Map ID (for single file import)")
    parser.add_argument("-c", "--clear", action="store_true", help="Clear database before import")
    parser.add_argument("--uri", default="bolt://localhost:7687", help="Neo4j URI")
    parser.add_argument("--user", default="neo4j", help="Neo4j user")
    parser.add_argument("--password", default="password", help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database")

    args = parser.parse_args()

    config = Neo4jConfig(
        uri=args.uri,
        user=args.user,
        password=args.password,
        database=args.database
    )

    adapter = Neo4jAdapter(config)
    adapter.connect()

    if args.clear:
        print("Clearing database...")
        adapter.clear_database()

    print("Creating indexes...")
    adapter.create_indexes()

    if args.input.is_dir():
        for f in args.input.glob("*.ttl"):
            map_id = f.stem
            print(f"Importing {map_id}...")
            nodes, rels = adapter.import_from_ttl(f, map_id)
            print(f"  Created {nodes} nodes, {rels} relationships")
    else:
        map_id = args.map_id or args.input.stem
        print(f"Importing {map_id}...")
        nodes, rels = adapter.import_from_ttl(args.input, map_id)
        print(f"Created {nodes} nodes, {rels} relationships")

    adapter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())