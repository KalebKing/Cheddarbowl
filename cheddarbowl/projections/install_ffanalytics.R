# Installs the ffanalytics package (official, actively-maintained repo) + deps.
# Prefer binary packages so we don't need Rtools to compile from source.
options(repos = c(CRAN = "https://cloud.r-project.org"), pkgType = "binary")

# The system library (C:\Program Files\R\...) is not writable without admin, so
# install into a writable per-project library (set via R_LIBS_USER by the caller).
lib <- Sys.getenv("R_LIBS_USER")
if (nzchar(lib)) {
  dir.create(lib, recursive = TRUE, showWarnings = FALSE)
  .libPaths(c(lib, .libPaths()))
}
cat("Installing into:", .libPaths()[1], "\n")

if (!requireNamespace("remotes", quietly = TRUE)) {
  install.packages("remotes", lib = .libPaths()[1])
}

remotes::install_github(
  "FantasyFootballAnalytics/ffanalytics",
  dependencies = TRUE,
  upgrade = "never",
  lib = .libPaths()[1]
)

ok <- requireNamespace("ffanalytics", quietly = TRUE)
cat(sprintf("\nINSTALL_DONE ffanalytics_available=%s\n", ok))
if (ok) cat("version:", as.character(packageVersion("ffanalytics")), "\n")
