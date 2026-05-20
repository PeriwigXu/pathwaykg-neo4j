#!/usr/bin/env python3

"""Build RDF knowledge graphs from KEGG data using Click CLI"""

import os
import sys
import tempfile
from pathlib import Path
from dotenv import load_dotenv
import click
from tqdm import tqdm
from Bio.KEGG import Compound
from rdflib.plugins.sparql import prepareQuery
from rdflib import Namespace, Graph, Literal, URIRef
import regex

import pathwaykg.namespaces as ns
from pathwaykg.fetch import fetch_pathway_kgml, parse_kgml, KGMLData, fetch_reaction_records, fetch_compound_records
from pathwaykg.neo4j_adapter import Neo4jAdapter, Neo4jConfig


class InvalidKEGGPathwayEntry(Exception):
    def __init__(self, s: str):
        super().__init__(f'Invalid KEGG pathway entry "{s}". A valid KEGG pathway entry consists of "ko" or a three-letter organism code, followed by five digits')


def add_reaction(graph: Graph, reaction_record: dict) -> None:
    """Add reaction with enzyme (EC) as core entity"""
    reaction_uri = ns.KEGG[reaction_record["id"]]
    graph.add((reaction_uri, ns.RDF.type, ns.KG["Reaction"]))
    graph.add((reaction_uri, ns.RDFS.label, Literal(reaction_record["definition"])))

    for id in reaction_record["substrates"]:
        compound_uri = ns.KEGG[id]
        graph.add((reaction_uri, ns.KG["hasSubstrate"], compound_uri))

    for id in reaction_record["products"]:
        compound_uri = ns.KEGG[id]
        graph.add((reaction_uri, ns.KG["hasProduct"], compound_uri))

    # Add Enzyme nodes via EC numbers
    for ec in reaction_record.get("enzymes", []):
        ec_uri = ns.EC[ec]
        graph.add((ec_uri, ns.RDF.type, ns.KG["Enzyme"]))
        graph.add((ec_uri, ns.RDFS.label, Literal(ec)))
        graph.add((reaction_uri, ns.KG["catalyzedBy"], ec_uri))


def add_compound(graph: Graph, compound_record: Compound.Record) -> None:
    compound_uri = ns.KEGG[compound_record.entry]
    graph.add((compound_uri, ns.RDF.type, ns.KG["Compound"]))
    # name is metadata - leave it out, Neo4j will use entity_id for display


def build_kg(organism_id: str, kgml_data: KGMLData) -> Graph:
    """Build knowledge graph with only what's in the pathway map: Enzyme, Reaction, Compound"""
    graph = Graph()
    graph.bind("kg", ns.KG)
    graph.bind("kegg", ns.KEGG)
    graph.bind("ec", ns.EC)

    # Fetch reaction records (includes EC numbers for enzymes)
    reaction_records = fetch_reaction_records(kgml_data.reaction_ids)
    for record in tqdm(reaction_records, total=len(kgml_data.reaction_ids), desc="Fetching reaction data", file=sys.stderr):
        add_reaction(graph, record)

    # Get all compounds involved in reactions
    q = prepareQuery("""
SELECT DISTINCT ?compound WHERE {
    { ?reaction kg:hasSubstrate ?compound }
    UNION
    { ?reaction kg:hasProduct ?compound }
}
""", initNs={"kg": ns.KG})

    compound_ids = {str(row.compound).split("/")[-1] for row in graph.query(q)}

    compound_records = fetch_compound_records(compound_ids)
    for record in tqdm(compound_records, total=len(compound_ids), desc="Fetching compound data", file=sys.stderr):
        add_compound(graph, record)

    return graph


def validate_pathway(pathway: str) -> tuple[str, str]:
    if len(pathway) == 7:
        organism, path_id = pathway[:2], pathway[2:]
        if organism == "ko":
            raise InvalidKEGGPathwayEntry(pathway)
    elif len(pathway) == 8:
        organism, path_id = pathway[:3], pathway[3:]
        if organism == "map":
            raise InvalidKEGGPathwayEntry(pathway)
    else:
        raise InvalidKEGGPathwayEntry(pathway)

    if not all(c.isalpha() for c in organism) or not all(c.isnumeric() for c in path_id):
        raise InvalidKEGGPathwayEntry(pathway)

    return organism, path_id


@click.group()
def cli():
    """Build RDF knowledge graphs from KEGG data"""
    pass


@cli.command()
@click.option("--pathway", "-p", required=True, help="KEGG pathway entry (e.g., hsa00010)")
def ttl(pathway):
    """Export to Turtle format"""
    try:
        organism, path_id = validate_pathway(pathway)
    except InvalidKEGGPathwayEntry as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    kgml_data = parse_kgml(fetch_pathway_kgml(organism, path_id))
    graph = build_kg(organism, kgml_data)

    graph.serialize(sys.stdout.buffer, format="turtle", encoding='utf-8')


@cli.group()
def neo4j():
    """Build and import to Neo4j"""
    pass


@neo4j.command("config")
@click.option("--show", is_flag=True, help="Show current Neo4j configuration from .env")
@click.option("--validate", is_flag=True, help="Validate Neo4j connection using .env config")
def neo4j_config(show, validate):
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


@neo4j.command("import")
@click.option("--pathway", "-p", required=True, help="KEGG pathway entry (e.g., hsa00010)")
@click.option("--uri", help="Neo4j URI (overrides .env)")
@click.option("--user", help="Neo4j user (overrides .env)")
@click.option("--password", help="Neo4j password (overrides .env)")
@click.option("--database", help="Neo4j database (overrides .env)")
@click.option("--clear", is_flag=True, help="Clear database before import")
def neo4j_import(pathway, uri, user, password, database, clear):
    """Build and import pathway to Neo4j"""
    load_dotenv()

    try:
        organism, path_id = validate_pathway(pathway)
    except InvalidKEGGPathwayEntry as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    kgml_data = parse_kgml(fetch_pathway_kgml(organism, path_id))
    graph = build_kg(organism, kgml_data)

    config = Neo4jConfig(
        uri=uri or os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        user=user or os.getenv("NEO4J_USER", "neo4j"),
        password=password or os.getenv("NEO4J_PASSWORD", "password"),
        database=database or os.getenv("NEO4J_DATABASE", "neo4j")
    )

    adapter = Neo4jAdapter(config)
    adapter.connect()

    if clear:
        click.echo("Clearing database...")
        adapter.clear_database()

    click.echo("Creating indexes...")
    adapter.create_indexes()

    ttl_path = Path(tempfile.gettempdir()) / f"{pathway}.ttl"
    graph.serialize(ttl_path, format="turtle", encoding='utf-8')

    nodes, rels = adapter.import_from_ttl(ttl_path, pathway)
    click.echo(f"Created {nodes} nodes, {rels} relationships")
    adapter.close()


def main():
    cli()


if __name__ == "__main__":
    main()