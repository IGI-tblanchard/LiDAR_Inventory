"""Add reprojected LiDAR DEM TIFFs to their client/UTM mosaic datasets."""

from __future__ import annotations

import csv
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

shared_inventory = Path(__file__).resolve().parents[2] / "UAV Updates" / "UAV-Inventory"
sys.path.insert(0, str(shared_inventory))
from task_run_report import RunReport

run_report = RunReport(__file__)
print(f"Detailed diagnostics: {run_report.path}")
try:
    import arcpy
except Exception as error:
    run_report.exception("import_arcpy", error)
    run_report.close()
    raise

BASE_DIR = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Client")
STAGING_GDB = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\LiDAR_Staging.gdb")
PRODUCTION_GDB = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Geodatabase\LiDAR_Mosaics.gdb")
REPORT_CSV = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\LiDAR_Reports\2_DEM_mosaic_add_report.csv")
CLIENT_FOLDERS = {"CVE": "Cenovus", "TOU": "Tourmaline", "WCP": "Whitecap"}
SOURCE_SUBDIRECTORY = Path("LiDAR") / "Aerial" / "BareEarth"
ZONE_FACTORY_CODES = {"10N": 3157, "11N": 2955, "12N": 2956}
FACTORY_CODE_ZONES = {code: zone for zone, code in ZONE_FACTORY_CODES.items()}
GEODATABASES = (("staging", STAGING_GDB), ("production", PRODUCTION_GDB))
REPORT_FIELDS = (
    "client", "project", "utm_zone", "target_crs", "input_path",
    "geodatabase", "mosaic_dataset", "status", "message",
)

try:
    arcpy.env.overwriteOutput = True
except Exception as error:
    run_report.exception("configure_arcpy_environment", error)
    run_report.close()
    raise


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def normalize_raster_path(path: str | Path) -> str:
    return str(path).replace("/", "\\").casefold()


def mosaic_name(client_folder: str, zone: str) -> str:
    return f"{client_folder}_UTM{zone[:2]}_DEM"


def iter_candidate_tifs():
    """Yield final BE_LiDAR TIFF outputs in each client's job folders."""
    for client_code, client_folder in CLIENT_FOLDERS.items():
        client_root = BASE_DIR / client_folder / SOURCE_SUBDIRECTORY
        if not client_root.is_dir():
            run_report.record(
                "discover_client_root", "warning", client=client_folder,
                input_path=client_root,
                message="Client BareEarth source directory is missing or inaccessible",
            )
            continue
        try:
            project_dirs = sorted(
                (path for path in client_root.iterdir() if path.is_dir()),
                key=lambda path: ((0, int(path.name)) if path.name.isdigit()
                                  else (1, path.name.casefold())),
            )
        except OSError as error:
            run_report.exception("discover_project_directories", error,
                                 client=client_folder, input_path=client_root)
            continue

        for project_dir in project_dirs:
            try:
                paths = sorted(
                    path for path in project_dir.glob("BE_LiDAR*.tif*")
                    if path.is_file()
                    and path.suffix.casefold() in {".tif", ".tiff"}
                    and ".tmp." not in path.name.casefold()
                    and not path.stem.casefold().endswith(".tmp")
                )
            except OSError as error:
                run_report.exception("discover_project_tifs", error,
                                     client=client_folder,
                                     project_or_job=project_dir.name,
                                     input_path=project_dir)
                continue
            for tif_path in paths:
                yield client_code, client_folder, project_dir.name, tif_path


def discover_inputs():
    """Group candidate TIFFs by client and their raster CRS-derived UTM zone."""
    grouped = defaultdict(list)
    candidate_count = 0
    for client_code, client_folder, project, tif_path in iter_candidate_tifs():
        candidate_count += 1
        try:
            sr = arcpy.Describe(str(tif_path)).spatialReference
            factory_code = int(sr.factoryCode)
            zone = FACTORY_CODE_ZONES.get(factory_code)
            if zone is None:
                raise ValueError(
                    f"Unsupported/undefined CRS {sr.name} (factory code {factory_code}); "
                    f"expected EPSG codes {sorted(FACTORY_CODE_ZONES)}"
                )
        except Exception as error:
            run_report.exception("inspect_input_raster", error, client=client_folder,
                                 project_or_job=project, data_type="DEM",
                                 input_path=tif_path)
            continue
        grouped[(client_code, client_folder, zone)].append((project, tif_path))

    if candidate_count == 0:
        run_report.record("discover_inputs", "warning", input_path=BASE_DIR,
                          message="No final BE_LiDAR TIFFs found under configured BareEarth roots")
    return grouped, candidate_count


def expected_mosaics():
    for code, client in CLIENT_FOLDERS.items():
        for zone, factory_code in ZONE_FACTORY_CODES.items():
            yield code, client, zone, factory_code, mosaic_name(client, zone)


def preflight_geodatabase(gdb: Path, grouped) -> None:
    """Ensure all nine mosaics, CRS values, and attribute fields are ready."""
    if not arcpy.Exists(str(gdb)):
        raise FileNotFoundError(f"Geodatabase is missing or inaccessible: {gdb}")
    arcpy.env.workspace = str(gdb)
    mosaic_names = set(arcpy.ListDatasets("*", "MosaicDataset") or [])

    for client_code, client_folder, zone, factory_code, name in expected_mosaics():
        mosaic_path = str(gdb / name)
        if name not in mosaic_names and not arcpy.Exists(mosaic_path):
            raise FileNotFoundError(f"Required mosaic is missing: {mosaic_path}")
        actual_code = int(arcpy.Describe(mosaic_path).spatialReference.factoryCode)
        if actual_code != factory_code:
            raise ValueError(
                f"{mosaic_path} has CRS factory code {actual_code}; expected {factory_code}"
            )

        catalog = str(gdb / f"AMD_{name}_CAT")
        if not arcpy.Exists(catalog):
            raise FileNotFoundError(f"Footprint catalog is missing: {catalog}")
        fields = {field.name.casefold(): field for field in arcpy.ListFields(catalog)}
        missing = {"path", "folderpath"} - fields.keys()
        if missing:
            raise ValueError(f"{catalog} is missing fields: {sorted(missing)}")

        input_rows = grouped.get((client_code, client_folder, zone), ())
        product_length = max((len(str(path)) for _, path in input_rows), default=0)
        group_length = max((len(str(path.parent)) for _, path in input_rows), default=0)
        if fields["path"].length < product_length:
            raise ValueError(
                f"{catalog} Path length {fields['path'].length} is too short; "
                f"current input paths need {product_length} characters (use 255)"
            )
        if fields["folderpath"].length < group_length:
            raise ValueError(
                f"{catalog} FolderPath length {fields['folderpath'].length} is too short; "
                f"current parent paths need {group_length} characters"
            )
    log(f"Preflight passed for all nine mosaics in {gdb}")


def mosaic_item_paths(mosaic_path: str) -> dict[int, str]:
    """Export the actual source path for each raster currently in a mosaic."""
    item_count = int(arcpy.management.GetCount(mosaic_path).getOutput(0))
    if item_count == 0:
        return {}
    path = Path(mosaic_path)
    table = arcpy.CreateUniqueName(f"dem_paths_{path.name}", str(path.parent))
    try:
        arcpy.management.ExportMosaicDatasetPaths(
            in_mosaic_dataset=mosaic_path, out_table=table,
            export_mode="ALL", types_of_paths="RASTER",
        )
        fields = {field.name.casefold(): field.name for field in arcpy.ListFields(table)}
        oid_field, path_field = fields.get("sourceoid"), fields.get("path")
        if not oid_field or not path_field:
            raise RuntimeError(f"Exported path table lacks SourceOID/Path fields: {table}")
        paths = {}
        with arcpy.da.SearchCursor(table, [oid_field, path_field]) as cursor:
            for oid, source in cursor:
                if source:
                    paths.setdefault(oid, str(source))
        return paths
    finally:
        if arcpy.Exists(table):
            arcpy.management.Delete(table)


def ensure_cell_size_ranges(mosaic_path: str) -> bool:
    """Calculate MinPS/MaxPS for mosaic items where either value is missing."""
    catalog = str(Path(mosaic_path).parent / f"AMD_{Path(mosaic_path).name}_CAT")
    try:
        fields = {field.name.casefold(): field.name for field in arcpy.ListFields(catalog)}
        min_field = fields.get("minps")
        max_field = fields.get("maxps")
        if not min_field or not max_field:
            raise RuntimeError(f"Footprint catalog is missing MinPS/MaxPS fields: {catalog}")

        with arcpy.da.SearchCursor(catalog, [min_field, max_field]) as cursor:
            missing_count = sum(
                1 for min_size, max_size in cursor
                if min_size is None or max_size is None
            )

        if missing_count == 0:
            run_report.record(
                "calculate_cell_size_ranges", "skipped", output_path=mosaic_path,
                message="All mosaic items already have MinPS/MaxPS values",
            )
            return True

        log(f"Calculating missing cell-size ranges for {missing_count} item(s): {mosaic_path}")
        arcpy.management.CalculateCellSizeRanges(
            in_mosaic_dataset=mosaic_path,
            do_compute_min="MIN_CELL_SIZES",
            do_compute_max="MAX_CELL_SIZES",
            update_missing_only="UPDATE_MISSING_ONLY",
        )

        with arcpy.da.SearchCursor(catalog, [min_field, max_field]) as cursor:
            remaining_count = sum(
                1 for min_size, max_size in cursor
                if min_size is None or max_size is None
            )
        if remaining_count:
            raise RuntimeError(
                f"Cell-size calculation left {remaining_count} item(s) without MinPS/MaxPS"
            )

        run_report.record(
            "calculate_cell_size_ranges", "completed", output_path=mosaic_path,
            message=f"Calculated missing MinPS/MaxPS values for {missing_count} item(s)",
        )
        log(f"Cell-size ranges populated for {missing_count} item(s): {mosaic_path}")
        return True
    except Exception as error:
        run_report.exception(
            "calculate_cell_size_ranges", error, output_path=mosaic_path,
            details=arcpy.GetMessages(2),
        )
        log(f"Cell-size range calculation failed for {mosaic_path}: {error}")
        return False


def add_tifs_to_mosaic(mosaic_path: str, rows, client_code: str, zone: str,
                       gdb_label: str, writer, report_handle) -> tuple[int, int]:
    """Add missing inputs, verify catalog membership, and set path attributes."""
    target_crs = f"EPSG:{ZONE_FACTORY_CODES[zone]}"
    try:
        item_paths = mosaic_item_paths(mosaic_path)
    except Exception as error:
        run_report.exception("export_existing_mosaic_paths", error, output_path=mosaic_path)
        message = f"Could not safely inspect mosaic paths: {error}"
        for project, tif in rows:
            writer.writerow(dict(client=client_code, project=project, utm_zone=zone,
                                 target_crs=target_crs, input_path=str(tif),
                                 geodatabase=gdb_label, mosaic_dataset=mosaic_path,
                                 status="check_failed", message=message))
        report_handle.flush()
        return len(rows), 0

    existing = {normalize_raster_path(path) for path in item_paths.values()}
    pending = []
    for project, tif in rows:
        if normalize_raster_path(tif) in existing:
            writer.writerow(dict(client=client_code, project=project, utm_zone=zone,
                                 target_crs=target_crs, input_path=str(tif),
                                 geodatabase=gdb_label, mosaic_dataset=mosaic_path,
                                 status="already_present",
                                 message="Source path already exists in mosaic catalog"))
        else:
            pending.append((project, tif))
    report_handle.flush()

    successful = []
    failed_count = 0
    for project, tif in pending:
        try:
            arcpy.management.AddRastersToMosaicDataset(
                in_mosaic_dataset=mosaic_path,
                raster_type="Raster Dataset",
                input_path=str(tif),
                update_cellsize_ranges="NO_CELL_SIZES",
                update_boundary="NO_BOUNDARY",
                update_overviews="NO_OVERVIEWS",
                duplicate_items_action="EXCLUDE_DUPLICATES",
                calculate_statistics="NO_STATISTICS",
                build_pyramids="NO_PYRAMIDS",
            )
            successful.append((project, tif))
        except Exception as error:
            failed_count += 1
            message = arcpy.GetMessages(2) or str(error)
            run_report.exception("add_raster_to_mosaic", error,
                                 project_or_job=project, data_type="DEM",
                                 input_path=tif, output_path=mosaic_path, details=message)
            writer.writerow(dict(client=client_code, project=project, utm_zone=zone,
                                 target_crs=target_crs, input_path=str(tif),
                                 geodatabase=gdb_label, mosaic_dataset=mosaic_path,
                                 status="failed", message=message))
        report_handle.flush()

    if successful:
        try:
            verified_paths = {
                normalize_raster_path(path)
                for path in mosaic_item_paths(mosaic_path).values()
            }
        except Exception as error:
            verified_paths = None
            run_report.exception("verify_mosaic_additions", error, output_path=mosaic_path)
        for project, tif in successful:
            verified = verified_paths is not None and normalize_raster_path(tif) in verified_paths
            status = "added" if verified else "verification_failed"
            message = "Source path found in mosaic catalog" if verified else "Added TIFF not verified in mosaic catalog"
            writer.writerow(dict(client=client_code, project=project, utm_zone=zone,
                                 target_crs=target_crs, input_path=str(tif),
                                 geodatabase=gdb_label, mosaic_dataset=mosaic_path,
                                 status=status, message="" if verified else message))
            run_report.record("verify_mosaic_addition", "completed" if verified else "failed",
                              project_or_job=project, data_type="DEM", input_path=tif,
                              output_path=mosaic_path, message=message)
            failed_count += int(not verified)
        report_handle.flush()

    try:
        item_paths = mosaic_item_paths(mosaic_path)
        catalog = str(Path(mosaic_path).parent / f"AMD_{Path(mosaic_path).name}_CAT")
        oid_field = arcpy.Describe(catalog).OIDFieldName
        updated = 0
        with arcpy.da.UpdateCursor(catalog, [oid_field, "Path", "FolderPath"]) as cursor:
            for row in cursor:
                source = item_paths.get(row[0])
                if source is None:
                    continue
                values = (source, str(Path(source).parent))
                if (row[1], row[2]) != values:
                    row[1], row[2] = values
                    cursor.updateRow(row)
                    updated += 1
        run_report.record("update_catalog_attributes", "completed", output_path=catalog,
                          message=f"Updated {updated} Path/FolderPath row(s)")
        log(f"Updated attributes on {updated} rows: {catalog}")
    except Exception as error:
        failed_count += 1
        run_report.exception("update_catalog_attributes", error,
                             output_path=mosaic_path, details=arcpy.GetMessages(2))

    log(f"{gdb_label} {mosaic_path}: added={len(successful)}, failed={failed_count}, already_present={len(rows)-len(pending)}")
    return failed_count, len(successful)


def build_production_overviews(mosaic_path: str) -> bool:
    """Build production overviews when a mosaic contains raster items."""
    try:
        item_count = int(arcpy.management.GetCount(mosaic_path).getOutput(0))
        if item_count == 0:
            message = "Mosaic dataset has no raster items; no overviews to build"
            run_report.record("build_production_overviews", "skipped",
                              output_path=mosaic_path, message=message)
            log(f"Production overviews skipped for empty mosaic: {mosaic_path}")
            return True

        arcpy.management.BuildOverviews(
            in_mosaic_dataset=mosaic_path,
            define_missing_tiles="DEFINE_MISSING_TILES",
            generate_overviews="GENERATE_OVERVIEWS",
            generate_missing_images="GENERATE_MISSING_IMAGES",
            regenerate_stale_images="REGENERATE_STALE_IMAGES",
        )
        run_report.record("build_production_overviews", "completed", output_path=mosaic_path,
                          message="Production overview build completed")
        log(f"Production overviews completed: {mosaic_path}")
        return True
    except Exception as error:
        run_report.exception("build_production_overviews", error,
                             output_path=mosaic_path, details=arcpy.GetMessages(2))
        log(f"Production overview build failed for {mosaic_path}: {error}")
        return False


def main() -> int:
    run_report.record("run", "started", message="DEM mosaic nightly update started",
                      details=f"staging={STAGING_GDB}; production={PRODUCTION_GDB}; roots={BASE_DIR}")
    grouped, candidate_count = discover_inputs()
    log(f"Discovered {candidate_count} candidates; CRS-routed {sum(map(len, grouped.values()))}")
    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    total_failed = 0
    total_added = 0
    with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as report_handle:
        writer = csv.DictWriter(report_handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        preflight_errors = []
        for label, gdb in GEODATABASES:
            try:
                preflight_geodatabase(gdb, grouped)
            except Exception as error:
                preflight_errors.append(f"{label}: {error}")
                run_report.exception("preflight_geodatabases", error, output_path=gdb,
                                     details=f"{label} preflight failed")
        if preflight_errors:
            raise RuntimeError("Preflight failed; no mosaic changes were made: " + " | ".join(preflight_errors))

        for label, gdb in GEODATABASES:
            arcpy.env.workspace = str(gdb)
            for client_code, client_folder, zone, _, name in expected_mosaics():
                rows = grouped.get((client_code, client_folder, zone), [])
                mosaic_path = str(gdb / name)
                if rows:
                    failed, added = add_tifs_to_mosaic(
                        mosaic_path, rows, client_code, zone, label, writer, report_handle
                    )
                    total_failed += failed
                    total_added += added
                else:
                    run_report.record("discover_mosaic_inputs", "info", client=client_folder,
                                      data_type="DEM", output_path=mosaic_path,
                                      message=f"No inputs for UTM zone {zone}")
                total_failed += int(not ensure_cell_size_ranges(mosaic_path))
            if label == "production":
                log("Building production overviews for all nine DEM mosaics")
                for _, _, _, _, name in expected_mosaics():
                    total_failed += int(not build_production_overviews(str(gdb / name)))

    run_report.record("run", "completed" if run_report.issue_count == 0 else "completed_with_issues",
                      output_path=REPORT_CSV,
                      message=f"candidates={candidate_count}; added={total_added}; failed={total_failed}")
    log(f"Report written: {REPORT_CSV}")
    log(f"Finished: added={total_added}; failed={total_failed}")
    return 1 if total_failed or run_report.issue_count else 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as error:
        log("Fatal error in DEM mosaic nightly update")
        log(str(error))
        log(traceback.format_exc())
        run_report.exception("run_fatal", error)
        print(f"Detailed diagnostics: {run_report.path}")
        run_report.close()
        sys.exit(1)
    print(f"Detailed diagnostics: {run_report.path}")
    run_report.close()
    sys.exit(exit_code)
