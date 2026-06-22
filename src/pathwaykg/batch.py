#!/usr/bin/env python3

"""Batch import KEGG pathways for an organism"""

import os
import sys
import tempfile
from pathlib import Path
from dotenv import load_dotenv
import click
from tqdm import tqdm
from Bio.KEGG import REST

from pathwaykg.fetch import fetch_pathway_kgml, parse_kgml, KGMLData, fetch_reaction_records, fetch_compound_records
from pathwaykg.build import kg_to_ttl, validate_pathway
from pathwaykg.neo4j_adapter import Neo4jAdapter, Neo4jConfig


def list_organism_pathways(organism: str) -> list[tuple[str, str]]:
    """List all pathways for an organism. Returns list of (pathway_id, name)"""
    response = REST.kegg_list("pathway", organism)
    pathways = []
    for line in response.read().splitlines():
        if line:
            parts = line.split("\t")
            if len(parts) == 2:
                pathway_id = parts[0].replace(f"{organism}", "")
                pathways.append((pathway_id, parts[1]))
    return pathways


def list_ko_pathways() -> list[tuple[str, str]]:
    """List all KO reference pathways. Returns list of (pathway_id, name)"""
    response = REST.kegg_list("pathway", "ko")
    pathways = []
    for line in response.read().splitlines():
        if line:
            parts = line.split("\t")
            if len(parts) == 2:
                pathway_id = parts[0].replace("ko", "")
                pathways.append((pathway_id, parts[1]))
    return pathways


@click.group()
def cli():
    """Build KEGG pathway knowledge graphs and import to Neo4j"""
    pass


@cli.command()
@click.option("--organism", "-o", required=True, help="Organism code (e.g., hsa, eco, sce)")
def list_pathways(organism):
    """List all pathways for an organism"""
    pathways = list_organism_pathways(organism)
    click.echo(f"Found {len(pathways)} pathways for {organism}:")
    for pathway_id, name in pathways:
        click.echo(f"  {organism}{pathway_id}\t{name}")


@cli.command()
@click.option("--organism", "-o", required=True, help="Organism code (e.g., hsa, eco, sce)")
@click.option("--clear", is_flag=True, help="Clear database before import")
def batch(organism, clear):
    """Import all pathways for an organism to Neo4j"""
    load_dotenv()

    pathways = list_organism_pathways(organism)
    if not pathways:
        click.echo(f"No pathways found for {organism}", err=True)
        raise SystemExit(1)

    click.echo(f"Found {len(pathways)} pathways for {organism}")

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

    total_nodes = 0
    total_rels = 0
    failed = []

    for pathway_id, name in tqdm(pathways, desc="Importing pathways"):
        full_pathway = f"{organism}{pathway_id}"
        try:
            kgml_data = parse_kgml(fetch_pathway_kgml(organism, pathway_id))
            graph = kg_to_ttl(kgml_data, full_pathway)

            ttl_path = Path(tempfile.gettempdir()) / f"{full_pathway}.ttl"
            graph.serialize(ttl_path, format="turtle", encoding='utf-8')

            nodes, rels = adapter.import_from_ttl(ttl_path, full_pathway)
            total_nodes += nodes
            total_rels += rels

            # Clean up temp file
            ttl_path.unlink(missing_ok=True)
        except Exception as e:
            failed.append((full_pathway, str(e)))
            tqdm.write(f"[FAIL] {full_pathway}: {e}")

    adapter.close()

    click.echo(f"\nImport complete: {total_nodes} nodes, {total_rels} relationships")
    if failed:
        click.echo(f"Failed pathways: {len(failed)}")
        for pathway, error in failed:
            click.echo(f"  {pathway}: {error}")
    else:
        click.echo("All pathways imported successfully!")


@cli.command()
@click.option("--clear", is_flag=True, help="Clear database before import")
def ko_batch(clear):
    """Import all KO reference pathways to Neo4j"""
    load_dotenv()

    pathways = list_ko_pathways()
    click.echo(f"Found {len(pathways)} KO reference pathways")

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

    total_nodes = 0
    total_rels = 0
    failed = []

    for pathway_id, name in tqdm(pathways, desc="Importing KO pathways"):
        full_pathway = f"ko{pathway_id}"
        try:
            kgml_data = parse_kgml(fetch_pathway_kgml("ko", pathway_id))
            graph = kg_to_ttl(kgml_data, full_pathway)

            ttl_path = Path(tempfile.gettempdir()) / f"{full_pathway}.ttl"
            graph.serialize(ttl_path, format="turtle", encoding='utf-8')

            nodes, rels = adapter.import_from_ttl(ttl_path, full_pathway)
            total_nodes += nodes
            total_rels += rels

            ttl_path.unlink(missing_ok=True)
        except Exception as e:
            failed.append((full_pathway, str(e)))
            tqdm.write(f"[FAIL] {full_pathway}: {e}")

    adapter.close()

    click.echo(f"\nImport complete: {total_nodes} nodes, {total_rels} relationships")
    if failed:
        click.echo(f"Failed pathways: {len(failed)}")
        for pathway, error in failed:
            click.echo(f"  {pathway}: {error}")
    else:
        click.echo("All KO pathways imported successfully!")


def main():
    cli()


if __name__ == "__main__":
    main()