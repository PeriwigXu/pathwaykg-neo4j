#!/usr/bin/env python3

"""Build and import KEGG pathway knowledge graphs to Neo4j"""

import os
import sys
import tempfile
from pathlib import Path
from dotenv import load_dotenv
import click
from tqdm import tqdm
from Bio.KEGG import Compound
from rdflib import Namespace, Literal, RDF, RDFS

# RDF Namespaces for TTL serialization
KEGG = Namespace("https://www.kegg.jp/entry/")
EC = Namespace("http://purl.uniprot.org/enzyme/")
KG = Namespace("https://github.com/eascarrunz/pathwaykg/ontology/")

from pathwaykg.fetch import fetch_pathway_kgml, parse_kgml, KGMLData, fetch_reaction_records, fetch_compound_records
from pathwaykg.neo4j_adapter import Neo4jAdapter, Neo4jConfig


class InvalidKEGGPathwayEntry(Exception):
    def __init__(self, s: str):
        super().__init__(f'Invalid KEGG pathway entry "{s}". A valid KEGG pathway entry consists of "ko" or a three-letter organism code, followed by five digits')


def validate_pathway(pathway: str) -> tuple[str, str]:
    if len(pathway) == 7:
        organism, path_id = pathway[:2], pathway[2:]
    elif len(pathway) == 8:
        organism, path_id = pathway[:3], pathway[3:]
        if organism == "map":
            raise InvalidKEGGPathwayEntry(pathway)
    else:
        raise InvalidKEGGPathwayEntry(pathway)

    if not all(c.isalpha() for c in organism) or not all(c.isnumeric() for c in path_id):
        raise InvalidKEGGPathwayEntry(pathway)

    return organism, path_id


def validate_ko_pathway(pathway: str) -> str:
    if not (len(pathway) == 7 and pathway[:2] == "ko" and pathway[2:].isdigit()):
        raise InvalidKEGGPathwayEntry(pathway)
    return pathway[2:]


def build_kg(organism_id: str, kgml_data: KGMLData) -> list[dict]:
    """Build knowledge graph data structure from KGML data.
    Returns list of node_data dicts for nodes in the pathway.
    """
    nodes = {}

    # Fetch reaction records (includes EC numbers for enzymes)
    reaction_records = fetch_reaction_records(kgml_data.reaction_ids)
    for record in tqdm(reaction_records, total=len(kgml_data.reaction_ids), desc="Fetching reaction data", file=sys.stderr):
        reaction_id = record["id"]
        if reaction_id not in nodes:
            nodes[reaction_id] = {
                "type": "Reaction",
                "kegg_id": reaction_id,
                "definition": record.get("definition", ""),
                "substrates": record.get("substrates", []),
                "products": record.get("products", []),
                "enzymes": record.get("enzymes", []),
            }
        else:
            # Merge data if node already exists
            nodes[reaction_id]["substrates"] = list(set(nodes[reaction_id].get("substrates", []) + record.get("substrates", [])))
            nodes[reaction_id]["products"] = list(set(nodes[reaction_id].get("products", []) + record.get("products", [])))
            nodes[reaction_id]["enzymes"] = list(set(nodes[reaction_id].get("enzymes", []) + record.get("enzymes", [])))

    # Collect compound IDs from all reactions
    compound_ids = set()
    for node in nodes.values():
        compound_ids.update(node.get("substrates", []))
        compound_ids.update(node.get("products", []))

    # Fetch compound records
    compound_records = fetch_compound_records(compound_ids)
    for record in compound_records:
        compound_id = record.entry
        if compound_id not in nodes:
            nodes[compound_id] = {
                "type": "Compound",
                "kegg_id": compound_id,
            }

    return list(nodes.values())


def build_ko_kg(kgml_data: KGMLData) -> list[dict]:
    """Build knowledge graph for KO (ortholog) pathways.
    Only includes EC enzymes and Reactions (no Gene/KOTerm nodes).
    """
    nodes = {}

    # Fetch reaction records
    reaction_records = fetch_reaction_records(kgml_data.reaction_ids)
    for record in tqdm(reaction_records, total=len(kgml_data.reaction_ids), desc="Fetching reaction data", file=sys.stderr):
        reaction_id = record["id"]
        if reaction_id not in nodes:
            nodes[reaction_id] = {
                "type": "Reaction",
                "kegg_id": reaction_id,
                "definition": record.get("definition", ""),
                "substrates": record.get("substrates", []),
                "products": record.get("products", []),
                "enzymes": record.get("enzymes", []),
            }
        else:
            nodes[reaction_id]["substrates"] = list(set(nodes[reaction_id].get("substrates", []) + record.get("substrates", [])))
            nodes[reaction_id]["products"] = list(set(nodes[reaction_id].get("products", []) + record.get("products", [])))
            nodes[reaction_id]["enzymes"] = list(set(nodes[reaction_id].get("enzymes", []) + record.get("enzymes", [])))

    # Collect compound IDs
    compound_ids = set()
    for node in nodes.values():
        compound_ids.update(node.get("substrates", []))
        compound_ids.update(node.get("products", []))

    compound_records = fetch_compound_records(compound_ids)
    for record in compound_records:
        compound_id = record.entry
        if compound_id not in nodes:
            nodes[compound_id] = {
                "type": "Compound",
                "kegg_id": compound_id,
            }

    return list(nodes.values())


def kg_to_ttl(kgml_data: KGMLData, map_id: str) -> Graph:
    """Convert KGML data to RDF Graph for Neo4j import.
    This creates a Turtle serialization for Neo4j to consume.
    """
    from rdflib import Graph as RDFGraph

    graph = RDFGraph()
    graph.bind("kg", KG)
    graph.bind("kegg", KEGG)
    graph.bind("ec", EC)

    for record in fetch_reaction_records(kgml_data.reaction_ids):
        reaction_uri = KEGG[record["id"]]
        graph.add((reaction_uri, RDF.type, KG["Reaction"]))
        graph.add((reaction_uri, RDFS.label, Literal(record.get("definition", ""))))

        for substrate_id in record.get("substrates", []):
            graph.add((reaction_uri, KG["hasSubstrate"], KEGG[substrate_id]))

        for product_id in record.get("products", []):
            graph.add((reaction_uri, KG["hasProduct"], KEGG[product_id]))

        for ec in record.get("enzymes", []):
            ec_uri = EC[ec]
            graph.add((ec_uri, RDF.type, KG["Enzyme"]))
            graph.add((ec_uri, RDFS.label, Literal(ec)))
            graph.add((reaction_uri, KG["catalyzedBy"], ec_uri))

    # Query all compounds
    from rdflib.plugins.sparql import prepareQuery
    q = prepareQuery("""
SELECT DISTINCT ?compound WHERE {
    { ?reaction kg:hasSubstrate ?compound }
    UNION
    { ?reaction kg:hasProduct ?compound }
}
""", initNs={"kg": KG})

    compound_ids = {str(row.compound).split("/")[-1] for row in graph.query(q)}

    for record in fetch_compound_records(compound_ids):
        compound_uri = KEGG[record.entry]
        graph.add((compound_uri, RDF.type, KG["Compound"]))

    return graph


@click.group()
def cli():
    """Build KEGG pathway knowledge graphs and import to Neo4j"""
    pass


@cli.command()
@click.option("--pathway", "-p", required=True, help="KEGG pathway entry (e.g., hsa00010)")
@click.option("--clear", is_flag=True, help="Clear database before import")
def import_cmd(pathway, clear):
    """Import pathway to Neo4j"""
    load_dotenv()

    try:
        organism, path_id = validate_pathway(pathway)
    except InvalidKEGGPathwayEntry as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    kgml_data = parse_kgml(fetch_pathway_kgml(organism, path_id))

    config = Neo4jConfig(
        uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_USER", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", "password"),
        database=os.getenv("NEO4J_DATABASE", "neo4j")
    )

    adapter = Neo4jAdapter(config)
    adapter.connect()

    if clear:
        click.echo("Clearing database...")
        adapter.clear_database()

    click.echo("Creating indexes...")
    adapter.create_indexes()

    ttl_path = Path(tempfile.gettempdir()) / f"{pathway}.ttl"
    graph = kg_to_ttl(kgml_data, pathway)
    graph.serialize(ttl_path, format="turtle", encoding='utf-8')

    nodes, rels = adapter.import_from_ttl(ttl_path, pathway)
    click.echo(f"Created {nodes} nodes, {rels} relationships")
    adapter.close()


@cli.command()
@click.option("--pathway", "-p", required=True, help="KEGG KO pathway entry (e.g., ko00010)")
@click.option("--clear", is_flag=True, help="Clear database before import")
def ko_import(pathway, clear):
    """Import KO (ortholog) pathway to Neo4j - EC enzymes and reactions only"""
    load_dotenv()

    try:
        path_id = validate_ko_pathway(pathway)
    except InvalidKEGGPathwayEntry as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    kgml_data = parse_kgml(fetch_pathway_kgml("ko", path_id))

    config = Neo4jConfig(
        uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_USER", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", "password"),
        database=os.getenv("NEO4J_DATABASE", "neo4j")
    )

    adapter = Neo4jAdapter(config)
    adapter.connect()

    if clear:
        click.echo("Clearing database...")
        adapter.clear_database()

    click.echo("Creating indexes...")
    adapter.create_indexes()

    ttl_path = Path(tempfile.gettempdir()) / f"{pathway}.ttl"
    graph = kg_to_ttl(kgml_data, pathway)
    graph.serialize(ttl_path, format="turtle", encoding='utf-8')

    nodes, rels = adapter.import_from_ttl(ttl_path, pathway)
    click.echo(f"Created {nodes} nodes, {rels} relationships")
    adapter.close()


@cli.command()
@click.option("--show", is_flag=True, help="Show current Neo4j configuration from .env")
@click.option("--validate", is_flag=True, help="Validate Neo4j connection using .env config")
def config(show, validate):
    """Manage Neo4j configuration from .env file"""
    load_dotenv()

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")
    database = os.getenv("NEO4J_DATABASE", "neo4j")

    if show:
        click.echo(f"NEO4J_URI={uri}")
        click.echo(f"NEO4J_USER={user}")
        click.echo("NEO4J_PASSWORD=***")
        click.echo(f"NEO4J_DATABASE={database}")
        return

    if validate:
        config = Neo4jConfig(uri=uri, user=user, password=password, database=database)
        adapter = Neo4jAdapter(config)
        try:
            adapter.connect()
            click.echo("[OK] Neo4j connection successful")
            adapter.close()
        except Exception as e:
            click.echo(f"[FAIL] Neo4j connection failed: {e}", err=True)
            raise SystemExit(1)
        return

    click.echo("Use --show or --validate")


def main():
    cli()


if __name__ == "__main__":
    main()
