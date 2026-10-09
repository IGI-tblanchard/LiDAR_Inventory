import csv
import math
import re
import sys
import time
from pathlib import Path

# Reuse the same timestamped diagnostics implementation as the UAV nightly jobs.
shared_inventory = Path(__file__).resolve().parents[2] / "UAV Updates" / "UAV-Inventory"
sys.path.insert(0, str(shared_inventory))
from task_run_report import RunReport

run_report = RunReport(__file__)
print(f"Detailed diagnostics: {run_report.path}")
try:
	import numpy as np
	from osgeo import gdal, ogr, osr
except Exception as error:
	run_report.exception("import_dependencies", error)
	run_report.close()
	raise


# Source paths mirror the inputs to 5_reproject_DEM_v3/v4, expressed as UNC
# paths so the script also works when launched by Windows Task Scheduler.
BASE_DIR = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Client")
CLIENT_FOLDERS = {
	"CVE": "Cenovus",
	"TOU": "Tourmaline",
	"WCP": "Whitecap",
}
SOURCE_SUBDIRECTORY = Path("LiDAR") / "Aerial" / "BareEarth"
REPORT_DIR = Path(r"\\IGG-QNAP12\IGG_Archive\IGG\Z_Drive\Staging\LiDAR_Reports")
REPORT_CSV = REPORT_DIR / "1_reproject_LiDAR_nightly_report.csv"

# UTM zones are determined from each source ADF's geographic extent, following
# 4_UTM_zone_check.py. These are the target CRS codes requested for nightly COGs.
UTM_ZONE_CRS = {
	"10N": "EPSG:3157",  # NAD83(CSRS) / UTM zone 10N
	"11N": "EPSG:2955",  # NAD83(CSRS) / UTM zone 11N
	"12N": "EPSG:2956",  # NAD83(CSRS) / UTM zone 12N
}

FALLBACK_NODATA_SENTINEL = 3.4028235e+38
SENTINEL_CLEANUP_THRESHOLD = 100000.0
RESAMPLING = gdal.GRA_Bilinear

try:
	gdal.UseExceptions()
	gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
	gdal.SetConfigOption("GDAL_CACHEMAX", "1024")
	gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
except Exception as error:
	run_report.exception("configure_gdal", error)
	run_report.close()
	raise


def log(message):
	print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}")


def natural_sort_key(value):
	return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value))]


def find_target_adf(folder):
	"""Find the GRID header file in a BEMOS folder (including nested GRID folders)."""
	direct_adf = folder / "w001001.adf"
	if direct_adf.is_file():
		return direct_adf
	try:
		return next(
			(path for path in folder.rglob("*") if path.is_file() and path.name.casefold() == "w001001.adf"),
			None,
		)
	except OSError as error:
		run_report.exception("discover_adf", error, input_path=folder)
		return None


def discover_sources():
	"""Yield BEMOS input folders in the same client/job layout used by v3/v4."""
	for client_code, client_folder in CLIENT_FOLDERS.items():
		source_root = BASE_DIR / client_folder / SOURCE_SUBDIRECTORY
		if not source_root.is_dir():
			run_report.record(
				"discover_source_root", "warning", client=client_folder,
				input_path=source_root, message="Configured BareEarth source root is missing or inaccessible",
			)
			continue

		try:
			job_dirs = sorted((path for path in source_root.iterdir() if path.is_dir()), key=natural_sort_key)
		except OSError as error:
			run_report.exception("discover_job_directories", error, client=client_folder,
								 input_path=source_root)
			continue

		for job_dir in job_dirs:
			try:
				bemos_dirs = sorted(
					(path for path in job_dir.iterdir()
					 if path.is_dir() and path.name.casefold().startswith("bemos")),
					key=natural_sort_key,
				)
			except OSError as error:
				run_report.exception("discover_bemos_directories", error, client=client_folder,
									 project_or_job=job_dir.name, input_path=job_dir)
				continue

			for index, bemos_dir in enumerate(bemos_dirs):
				yield client_code, client_folder, job_dir.name, index, bemos_dir, find_target_adf(bemos_dir)


def get_utm_zone_and_target_crs(adf_path):
	"""Project the ADF's footprint to WGS84 and determine its UTM zone."""
	dataset = gdal.Open(str(adf_path))
	if dataset is None:
		dataset = gdal.Open(str(adf_path.parent))
	if dataset is None:
		raise RuntimeError(f"GDAL could not open the ADF raster or its GRID folder: {adf_path}")

	projection_wkt = dataset.GetProjection()
	if not projection_wkt:
		dataset = None
		raise ValueError(f"Source raster has no coordinate reference system: {adf_path}")

	source_srs = osr.SpatialReference()
	if source_srs.ImportFromWkt(projection_wkt) != 0:
		dataset = None
		raise ValueError(f"Could not parse source CRS: {adf_path}")
	source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

	wgs84 = osr.SpatialReference()
	wgs84.ImportFromEPSG(4326)
	wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
	transform = osr.CoordinateTransformation(source_srs, wgs84)

	x_origin, pixel_width, row_rotation, y_origin, column_rotation, pixel_height = dataset.GetGeoTransform()
	width, height = dataset.RasterXSize, dataset.RasterYSize
	corners = (
		(x_origin, y_origin),
		(x_origin + width * pixel_width, y_origin + width * column_rotation),
		(x_origin + width * pixel_width + height * row_rotation,
		 y_origin + width * column_rotation + height * pixel_height),
		(x_origin + height * row_rotation, y_origin + height * pixel_height),
	)
	ring = ogr.Geometry(ogr.wkbLinearRing)
	for x, y in (*corners, corners[0]):
		ring.AddPoint_2D(x, y)
	polygon = ogr.Geometry(ogr.wkbPolygon)
	polygon.AddGeometry(ring)
	if polygon.Transform(transform) != 0:
		dataset = None
		raise RuntimeError(f"Could not project raster footprint to WGS84: {adf_path}")

	min_lon, max_lon, min_lat, max_lat = polygon.GetEnvelope()
	dataset = None
	center_lon = (min_lon + max_lon) / 2
	center_lat = (min_lat + max_lat) / 2
	if not (-180 <= center_lon <= 180 and -90 <= center_lat <= 90):
		raise ValueError(f"Invalid WGS84 footprint center ({center_lat}, {center_lon}) for {adf_path}")

	zone_number = int((center_lon + 180) / 6) + 1
	hemisphere = "N" if center_lat >= 0 else "S"
	zone = f"{zone_number}{hemisphere}"
	target_crs = UTM_ZONE_CRS.get(zone)
	if target_crs is None:
		raise ValueError(f"UTM zone {zone} is not configured; supported zones: {', '.join(UTM_ZONE_CRS)}")
	return zone, target_crs, center_lat, center_lon


def clean_sentinel_values(dataset, threshold=SENTINEL_CLEANUP_THRESHOLD, nodata_value=-9999.0):
	"""Replace extreme fill/sentinel pixels block-wise with the output NoData value."""
	for band_index in range(1, dataset.RasterCount + 1):
		band = dataset.GetRasterBand(band_index)
		block_x, block_y = band.GetBlockSize()
		for y in range(0, band.YSize, block_y):
			rows = min(block_y, band.YSize - y)
			for x in range(0, band.XSize, block_x):
				columns = min(block_x, band.XSize - x)
				values = band.ReadAsArray(x, y, columns, rows)
				mask = np.abs(values) > threshold
				if mask.any():
					values[mask] = nodata_value
					band.WriteArray(values, x, y)


def create_cog_from_adf(adf_path, output_path, target_crs):
	"""Reproject an ADF GRID to a one-metre Float32 COG using the v4 pipeline."""
	source = gdal.Open(str(adf_path))
	if source is None:
		source = gdal.Open(str(adf_path.parent))
	if source is None:
		raise RuntimeError(f"Could not open source raster: {adf_path}")

	src_nodata = source.GetRasterBand(1).GetNoDataValue()
	if src_nodata is None:
		src_nodata = FALLBACK_NODATA_SENTINEL
		run_report.record("inspect_source_raster", "warning", input_path=adf_path,
						  output_path=output_path,
						  message="Source has no NoData tag; fallback sentinel used")

	target_srs = osr.SpatialReference()
	target_srs.ImportFromEPSG(int(target_crs.split(":")[1]))
	target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
	vrt = gdal.AutoCreateWarpedVRT(source, None, target_srs.ExportToWkt(), RESAMPLING)
	if vrt is None:
		source = None
		raise RuntimeError(f"Could not calculate output dimensions: {adf_path}")

	width, height = vrt.RasterXSize, vrt.RasterYSize
	overview_count = max(1, math.ceil(math.log2(max(width, height) / 256))) if max(width, height) > 256 else 1
	vrt = None

	options = gdal.WarpOptions(
		format="COG",
		dstSRS=target_crs,
		resampleAlg=RESAMPLING,
		xRes=1,
		yRes=1,
		outputType=gdal.GDT_Float32,
		srcNodata=src_nodata,
		dstNodata=-9999,
		warpOptions=["INIT_DEST=-9999"],
		creationOptions=[
			"COMPRESS=LZW", "PREDICTOR=2", "BLOCKSIZE=512",
			f"OVERVIEW_COUNT={overview_count}", "OVERVIEW_RESAMPLING=BILINEAR",
			"NUM_THREADS=ALL_CPUS", "BIGTIFF=IF_SAFER", "STATISTICS=YES",
		],
		callback=gdal.TermProgress_nocb,
	)
	output = gdal.Warp(str(output_path), source, options=options)
	source = None
	if output is None:
		raise RuntimeError(f"GDAL Warp did not create the output COG: {output_path}")

	clean_sentinel_values(output)
	for band_index in range(1, output.RasterCount + 1):
		output.GetRasterBand(band_index).ComputeStatistics(False)
	output.FlushCache()
	output = None
	return True


def is_valid_existing_output(path):
	"""Only treat an existing output as complete if GDAL can open a nonempty raster."""
	try:
		if not path.is_file() or path.stat().st_size == 0:
			return False
		dataset = gdal.Open(str(path))
		if dataset is None:
			return False
		valid = dataset.RasterXSize > 0 and dataset.RasterYSize > 0
		dataset = None
		return valid
	except Exception:
		return False


def output_path_for(job_dir, job_number, bemos_index):
	"""Preserve v3/v4 naming: BE_LiDAR_<job>.tif, then _1, _2, etc."""
	suffix = f"_{bemos_index}" if bemos_index else ""
	return job_dir / f"BE_LiDAR_{job_number}{suffix}.tif"


def main():
	run_report.record("run", "started", message="LiDAR DEM rename/naming and reprojection run started",
					  details=f"BASE_DIR={BASE_DIR}; target_crs_by_zone={UTM_ZONE_CRS}")
	sources = list(discover_sources())
	if not sources:
		raise FileNotFoundError(f"No BEMOS source folders found under configured client roots in {BASE_DIR}")

	processed = 0
	already_done = 0
	failed = 0
	with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as report_handle:
		fields = ["client", "project", "bemos_folder", "utm_zone", "target_crs",
				  "input_path", "output_file_path", "status", "message"]
		writer = csv.DictWriter(report_handle, fieldnames=fields)
		writer.writeheader()

		for client_code, client_folder, job_number, bemos_index, bemos_dir, adf_path in sources:
			output_path = output_path_for(bemos_dir.parent, job_number, bemos_index)
			row = {
				"client": client_code,
				"project": job_number,
				"bemos_folder": bemos_dir.name,
				"utm_zone": "",
				"target_crs": "",
				"input_path": str(bemos_dir),
				"output_file_path": str(output_path),
				"status": "",
				"message": "",
			}

			if adf_path is None:
				message = "No w001001.adf raster found in the BEMOS folder"
				row.update(status="missing_adf", message=message)
				writer.writerow(row)
				report_handle.flush()
				failed += 1
				run_report.record("discover_adf", "failed", client=client_folder,
								  project_or_job=job_number, input_path=bemos_dir,
								  output_path=output_path, message=message)
				continue

			try:
				zone, target_crs, center_lat, center_lon = get_utm_zone_and_target_crs(adf_path)
				row["utm_zone"] = zone
				row["target_crs"] = target_crs
				run_report.record("determine_utm_zone", "completed", client=client_folder,
								  project_or_job=job_number, data_type="DTM", input_path=adf_path,
								  output_path=output_path,
								  message=f"Zone {zone} -> {target_crs}; center={center_lat:.6f},{center_lon:.6f}")
			except Exception as error:
				row.update(status="crs_failed", message=f"{type(error).__name__}: {error}")
				writer.writerow(row)
				report_handle.flush()
				failed += 1
				run_report.exception("determine_utm_zone", error, client=client_folder,
									 project_or_job=job_number, input_path=adf_path,
									 output_path=output_path)
				continue

			if is_valid_existing_output(output_path):
				row.update(status="skipped_already_exists",
						   message="Existing output opened successfully; reprojection skipped")
				already_done += 1
				run_report.record("reprojection", "skipped", client=client_folder,
								  project_or_job=job_number, data_type="DTM", input_path=adf_path,
								  output_path=output_path, message=row["message"])
			else:
				try:
					if output_path.exists():
						output_path.unlink()
					log(f"REPROJECT {adf_path} -> {output_path} ({zone}, {target_crs})")
					create_cog_from_adf(adf_path, output_path, target_crs)
					row.update(status="completed", message="COG created successfully")
					processed += 1
					run_report.record("reprojection", "completed", client=client_folder,
									  project_or_job=job_number, data_type="DTM", input_path=adf_path,
									  output_path=output_path, message=f"COG created in {target_crs}")
				except Exception as error:
					row.update(status="failed", message=f"{type(error).__name__}: {error}")
					failed += 1
					run_report.exception("reprojection", error, client=client_folder,
										 project_or_job=job_number, data_type="DTM",
										 input_path=adf_path, output_path=output_path)

			writer.writerow(row)
			report_handle.flush()

	log(f"Report written: {REPORT_CSV}")
	log(f"Finished: {processed} COGs created, {already_done} already done, {failed} failed")
	run_report.record("run", "completed" if failed == 0 else "completed_with_issues",
					  output_path=REPORT_CSV,
					  message=f"processed={processed}; already_done={already_done}; failed={failed}")
	return 1 if run_report.issue_count else 0


if __name__ == "__main__":
	try:
		exit_code = main()
	except Exception as error:
		log("Fatal error in LiDAR nightly reprojection")
		log(str(error))
		run_report.exception("run_fatal", error)
		print(f"Detailed diagnostics: {run_report.path}")
		run_report.close()
		sys.exit(1)
	print(f"Detailed diagnostics: {run_report.path}")
	run_report.close()
	sys.exit(exit_code)

