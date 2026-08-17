# Scrape projections via ffanalytics, score them half-PPR, attach ADP + player info,
# and write a tidy CSV the Python strategy engine consumes.
#
# Usage (via config.run_rscript, which sets R_LIBS_USER):
#   Rscript run_projections.R [season] [week] [out_csv]
#   defaults: season=2026 week=0 out=<repo>/data/seasons/<season>/projections/preseason.csv
#   (callers normally pass out_csv explicitly = config.PROJECTIONS_CSV)

lib <- Sys.getenv("R_LIBS_USER")
if (nzchar(lib)) .libPaths(c(lib, .libPaths()))
suppressWarnings(suppressMessages(library(ffanalytics)))

args   <- commandArgs(trailingOnly = TRUE)
season <- as.integer(if (length(args) >= 1) args[1] else 2026)
week   <- as.integer(if (length(args) >= 2) args[2] else 0)
script_dir <- dirname(sub("--file=", "", grep("--file=", commandArgs(FALSE), value = TRUE)))
out_csv <- if (length(args) >= 3) args[3] else file.path(
  script_dir, "..", "..", "data", "seasons", as.character(season), "projections", "preseason.csv")

# --- Half-PPR scoring: start from the package default (standard) and set rec = 0.5 ---
half_ppr <- scoring
half_ppr$rec$rec <- 0.5

# Offense + K aggregate cleanly across many sources. DST aggregation errors when
# certain sources are included, so DST is scraped separately from known-good sources.
OFF_SOURCES <- c("CBS", "ESPN", "FantasyPros", "FantasySharks", "FFToday", "NumberFire", "NFL")
OFF_POS     <- c("QB", "RB", "WR", "TE", "K")
DST_SOURCES <- c("CBS", "ESPN", "FFToday", "FantasySharks")

build_table <- function(src, pos, avg) {
  s <- scrape_data(src = src, pos = pos, season = season, week = week)
  t <- projections_table(s, scoring_rules = half_ppr, avg_type = avg)
  t <- add_player_info(t)
  t <- tryCatch(add_adp(t), error = function(e) { cat("add_adp failed:", conditionMessage(e), "\n"); t })
  t
}

cat("Scraping offense + K...\n")
proj <- build_table(OFF_SOURCES, OFF_POS, "weighted")

cat("Scraping DST separately...\n")
dst <- tryCatch(build_table(DST_SOURCES, c("DST"), "average"),
                error = function(e) { cat("DST scrape failed:", conditionMessage(e), "\n"); NULL })
if (!is.null(dst) && nrow(dst) > 0) {
  common <- intersect(colnames(proj), colnames(dst))
  proj <- rbind(proj[, common], dst[, common])
  cat("Added", nrow(dst), "DST rows.\n")
}

cat("Columns produced:", paste(colnames(proj), collapse = ", "), "\n")
cat("Rows:", nrow(proj), "\n")

dir.create(dirname(out_csv), showWarnings = FALSE, recursive = TRUE)
# Write the full table; the Python loader selects/renames what it needs. This is
# robust to ffanalytics column changes across versions.
write.csv(proj, out_csv, row.names = FALSE, na = "")
cat("Wrote", normalizePath(out_csv), "\n")
