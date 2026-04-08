require([
  "esri/Map",
  "esri/views/MapView",
  "esri/layers/FeatureLayer",
  "esri/layers/GraphicsLayer",
  "esri/layers/VectorTileLayer",
  "esri/geometry/Extent",
  "esri/geometry/Polyline",
  "esri/widgets/Home",
  "esri/widgets/Expand",
  "esri/widgets/LayerList",
], function (
  EsriMap,
  MapView,
  FeatureLayer,
  GraphicsLayer,
  VectorTileLayer,
  Extent,
  Polyline,
  Home,
  Expand,
  LayerList,
) {
  const SEGMENTS_URL =
    "https://services9.arcgis.com/eNX73FDxjlKFtCtH/arcgis/rest/services/FTW_Segmentation_Master/FeatureServer/0";
  const TXDOT_VECTOR_URL =
    "https://tiles.arcgis.com/tiles/KTcxiTD9dsQw4r7Z/arcgis/rest/services/TxDOT_Vector_Tile_Basemap/VectorTileServer";
  const TXDOT_ROADS_URL =
    "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services/TxDOT_Roadways/FeatureServer/0";
  const HIGHWAY_DESIGNATION_BASE_URL = "https://www.dot.state.tx.us/tpp/hdf_search.html";

  const state = {
    activeCategory: "All",
    searchTerm: "",
    selectedSegmentIds: new Set(),
    segments: [],
  };

  const elements = {
    categoryList: document.getElementById("category-list"),
    segmentList: document.getElementById("segment-list"),
    searchInput: document.getElementById("segment-search"),
    visibleCount: document.getElementById("visible-count"),
    selectedCount: document.getElementById("selected-count"),
    clearSelectionButton: document.getElementById("clear-selection"),
    zoomSelectedButton: document.getElementById("zoom-selected"),
  };

  const txdotVectorLayer = new VectorTileLayer({
    url: TXDOT_VECTOR_URL,
    title: "TxDOT Roadways",
  });

  const segmentsLayer = new FeatureLayer({
    url: SEGMENTS_URL,
    title: "FTW Segments",
    outFields: [
      "OBJECTID",
      "Readable_SegID",
      "Segment_ID",
      "Highway",
      "County",
      "HSYS",
      "Segment_Length_Mi",
      "Need_Description",
      "Type_of_Need",
    ],
    popupEnabled: false,
    renderer: {
      type: "simple",
      symbol: {
        type: "simple-line",
        color: [158, 50, 82, 230],
        width: 3,
        cap: "round",
        join: "round",
      },
    },
  });

  const selectedSegmentsLayer = new GraphicsLayer({
    title: "Selected FTW Segments",
    listMode: "hide",
  });

  const roadsQueryLayer = new FeatureLayer({
    url: TXDOT_ROADS_URL,
    outFields: [
      "OBJECTID",
      "RTE_NM",
      "RTE_PRFX",
      "RTE_NBR",
      "MAP_LBL",
      "RDBD_TYPE",
      "DES_DRCT",
      "COUNTY",
      "BEGIN_DFO",
      "END_DFO",
    ],
    popupEnabled: false,
  });

  const map = new EsriMap({
    basemap: "gray-vector",
    layers: [txdotVectorLayer, segmentsLayer, selectedSegmentsLayer],
  });

  const view = new MapView({
    container: "viewDiv",
    map,
    center: [-97.45, 32.72],
    zoom: 9,
    constraints: {
      snapToZoom: false,
    },
    popup: {
      dockEnabled: true,
      dockOptions: {
        position: "bottom-right",
        breakpoint: false,
      },
    },
  });

  window.__mapView = view;

  view.ui.add(new Home({ view }), "top-left");

  const layerList = new LayerList({
    view,
    listItemCreatedFunction(event) {
      const item = event.item;
      if (item.layer === selectedSegmentsLayer) {
        item.hidden = true;
      }
    },
  });

  view.ui.add(
    new Expand({
      view,
      content: layerList,
      expandTooltip: "Layer list",
    }),
    "top-right",
  );

  const sortByLabel = (a, b) => a.label.localeCompare(b.label, undefined, { numeric: true });

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function getVisibleSegments() {
    const normalizedSearch = state.searchTerm.trim().toLowerCase();

    return state.segments.filter((segment) => {
      const matchesCategory =
        state.activeCategory === "All" || segment.hsys === state.activeCategory;

      if (!matchesCategory) {
        return false;
      }

      if (!normalizedSearch) {
        return true;
      }

      const haystack = [
        segment.label,
        segment.segmentId,
        segment.highway,
        segment.county,
        segment.hsys,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();

      return haystack.includes(normalizedSearch);
    });
  }

  function getCategorySummaries() {
    const counts = new globalThis.Map([["All", state.segments.length]]);

    state.segments.forEach((segment) => {
      counts.set(segment.hsys, (counts.get(segment.hsys) ?? 0) + 1);
    });

    const summaries = Array.from(counts.entries()).map(([code, count]) => ({
      code,
      count,
      label: code === "All" ? "All segments" : code,
      subtitle: code === "All" ? "Entire FTW inventory" : "Highway system",
    }));

    return [summaries[0], ...summaries.slice(1).sort(sortByLabel)];
  }

  function applyCategoryFilter() {
    segmentsLayer.definitionExpression =
      state.activeCategory === "All" ? null : "HSYS = '" + state.activeCategory.replaceAll("'", "''") + "'";

    const allowedIds =
      state.activeCategory === "All"
        ? null
        : new Set(
            state.segments
              .filter((segment) => segment.hsys === state.activeCategory)
              .map((segment) => segment.objectId),
          );

    if (allowedIds) {
      state.selectedSegmentIds = new Set(
        Array.from(state.selectedSegmentIds).filter((objectId) => allowedIds.has(objectId)),
      );
    }
  }

  function renderCategories() {
    const summaries = getCategorySummaries();

    elements.categoryList.innerHTML = summaries
      .map(
        (summary) => `
          <button
            type="button"
            class="category-button ${summary.code === state.activeCategory ? "is-active" : ""}"
            data-category="${escapeHtml(summary.code)}"
          >
            <span class="category-label">
              <span class="category-code">${escapeHtml(summary.label)}</span>
              <span class="category-subtitle">${escapeHtml(summary.subtitle)}</span>
            </span>
            <span class="count-pill">${summary.count}</span>
          </button>
        `,
      )
      .join("");

    elements.categoryList.querySelectorAll("[data-category]").forEach((button) => {
      button.addEventListener("click", () => {
        state.activeCategory = button.dataset.category;
        applyCategoryFilter();
        render();
        syncSelectedGraphics();
      });
    });
  }

  function renderSegments() {
    const visibleSegments = getVisibleSegments();

    elements.visibleCount.textContent = String(visibleSegments.length);
    elements.selectedCount.textContent = String(state.selectedSegmentIds.size);
    elements.clearSelectionButton.disabled = state.selectedSegmentIds.size === 0;
    elements.zoomSelectedButton.disabled = state.selectedSegmentIds.size === 0;

    if (!visibleSegments.length) {
      elements.segmentList.innerHTML =
        '<div class="empty-state">No segments match the current filter.</div>';
      return;
    }

    elements.segmentList.innerHTML = visibleSegments
      .map((segment) => {
        const isSelected = state.selectedSegmentIds.has(segment.objectId);

        return `
          <article class="segment-card ${isSelected ? "is-selected" : ""}" data-segment-id="${segment.objectId}">
            <input
              class="segment-toggle"
              type="checkbox"
              data-toggle-id="${segment.objectId}"
              ${isSelected ? "checked" : ""}
              aria-label="Select ${escapeHtml(segment.label)}"
            />

            <div class="segment-meta" data-focus-id="${segment.objectId}">
              <h3 class="segment-name">${escapeHtml(segment.label)}</h3>
              <p class="segment-detail">${escapeHtml(segment.highway || "Unknown highway")} · ${escapeHtml(
                segment.county || "County unavailable",
              )}</p>
              <div class="segment-tags">
                <span class="tag">${escapeHtml(segment.hsys || "N/A")}</span>
                <span class="tag">${escapeHtml(formatMiles(segment.lengthMiles))}</span>
              </div>
            </div>

            <button class="focus-button" type="button" data-focus-id="${segment.objectId}">
              Focus
            </button>
          </article>
        `;
      })
      .join("");

    elements.segmentList.querySelectorAll("[data-toggle-id]").forEach((checkbox) => {
      checkbox.addEventListener("click", (event) => {
        event.stopPropagation();
      });

      checkbox.addEventListener("change", async () => {
        const objectId = Number(checkbox.dataset.toggleId);

        if (checkbox.checked) {
          state.selectedSegmentIds.add(objectId);
        } else {
          state.selectedSegmentIds.delete(objectId);
        }

        render();
        await syncSelectedGraphics();
      });
    });

    elements.segmentList.querySelectorAll("[data-focus-id]").forEach((element) => {
      element.addEventListener("click", async () => {
        const objectId = Number(element.dataset.focusId);
        state.selectedSegmentIds.add(objectId);
        render();
        await syncSelectedGraphics();
        await zoomToSegments([objectId]);
      });
    });
  }

  function render() {
    renderCategories();
    renderSegments();
  }

  function formatMiles(lengthMiles) {
    if (typeof lengthMiles !== "number" || Number.isNaN(lengthMiles)) {
      return "Length unavailable";
    }

    return lengthMiles.toFixed(1) + " mi";
  }

  async function querySegments(objectIds) {
    if (!objectIds.length) {
      return [];
    }

    const query = segmentsLayer.createQuery();
    query.objectIds = objectIds;
    query.returnGeometry = true;
    query.outFields = [
      "OBJECTID",
      "Readable_SegID",
      "Segment_ID",
      "Highway",
      "County",
      "HSYS",
      "Segment_Length_Mi",
      "Need_Description",
      "Type_of_Need",
    ];

    const result = await segmentsLayer.queryFeatures(query);
    return result.features;
  }

  function buildZoomExtent(features) {
    const extents = features.map((feature) => feature.geometry?.extent).filter(Boolean);

    if (!extents.length) {
      return null;
    }

    const bounds = extents.reduce(
      (accumulator, extent) => ({
        xmin: Math.min(accumulator.xmin, extent.xmin),
        ymin: Math.min(accumulator.ymin, extent.ymin),
        xmax: Math.max(accumulator.xmax, extent.xmax),
        ymax: Math.max(accumulator.ymax, extent.ymax),
      }),
      {
        xmin: Number.POSITIVE_INFINITY,
        ymin: Number.POSITIVE_INFINITY,
        xmax: Number.NEGATIVE_INFINITY,
        ymax: Number.NEGATIVE_INFINITY,
      },
    );

    const width = Math.max(bounds.xmax - bounds.xmin, 1);
    const height = Math.max(bounds.ymax - bounds.ymin, 1);
    const isSingleSegment = features.length === 1;
    const horizontalPadding = Math.max(width * (isSingleSegment ? 0.28 : 0.18), isSingleSegment ? 1800 : 900);
    const verticalPadding = Math.max(height * (isSingleSegment ? 0.55 : 0.35), isSingleSegment ? 1800 : 900);

    // Long, shallow segment extents still fit too tightly unless we expand beyond raw bounds.
    return new Extent({
      xmin: bounds.xmin - horizontalPadding,
      ymin: bounds.ymin - verticalPadding,
      xmax: bounds.xmax + horizontalPadding,
      ymax: bounds.ymax + verticalPadding,
      spatialReference: extents[0].spatialReference,
    }).expand(isSingleSegment ? 1.2 : 1.1);
  }

  function buildSegmentPopup(feature) {
    const attributes = feature.attributes ?? {};
    const name = attributes.Readable_SegID || attributes.Segment_ID || "FTW segment";
    const lengthText = formatMiles(attributes.Segment_Length_Mi);
    const highway = attributes.Highway || "Unknown highway";
    const county = attributes.County || "County unavailable";
    const typeOfNeed = attributes.Type_of_Need || "Need type not provided";

    return {
      title: name,
      content: `
        <div class="road-popup">
          <p><strong>${escapeHtml(highway)}</strong> · ${escapeHtml(county)}</p>
          <ul>
            <li>${escapeHtml(lengthText)}</li>
            <li>${escapeHtml(typeOfNeed)}</li>
          </ul>
        </div>
      `,
    };
  }

  /**
   * Filter out small artifact fragments from a polyline geometry.
   * Keeps paths that are >= minLength meters OR >= minRatio of total length.
   * Connected paths (endpoint gap < mergeThreshold) are kept regardless of size.
   */
  function filterArtifactPaths(geometry, { minLength = 600, minRatio = 0.05, mergeThreshold = 50 } = {}) {
    if (!geometry || !geometry.paths || geometry.paths.length <= 1) {
      return geometry;
    }

    const paths = geometry.paths;

    // Compute path lengths (approximate using Euclidean distance in map units)
    function pathLength(path) {
      let len = 0;
      for (let i = 1; i < path.length; i++) {
        const dx = path[i][0] - path[i - 1][0];
        const dy = path[i][1] - path[i - 1][1];
        len += Math.sqrt(dx * dx + dy * dy);
      }
      return len;
    }

    // Distance between two points
    function ptDist(a, b) {
      const dx = a[0] - b[0];
      const dy = a[1] - b[1];
      return Math.sqrt(dx * dx + dy * dy);
    }

    // Minimum endpoint-to-endpoint distance between two paths
    function endpointGap(pathA, pathB) {
      const aStart = pathA[0];
      const aEnd = pathA[pathA.length - 1];
      const bStart = pathB[0];
      const bEnd = pathB[pathB.length - 1];
      return Math.min(
        ptDist(aStart, bStart), ptDist(aStart, bEnd),
        ptDist(aEnd, bStart), ptDist(aEnd, bEnd),
      );
    }

    // Check if path is connected to any other path (gap < mergeThreshold)
    // Connected paths are kept even if small
    const lengths = paths.map(pathLength);
    const totalLength = lengths.reduce((a, b) => a + b, 0);

    // Convert mergeThreshold from meters to approximate map units
    // For WGS84 at ~32° latitude: 1 degree ≈ 85km lon, 111km lat
    // Map units are degrees, so 50m ≈ 0.00045 degrees
    const sr = geometry.spatialReference;
    const isGeographic = !sr || sr.isGeographic || sr.wkid === 4326;
    const thresholdUnits = isGeographic ? mergeThreshold / 100000 : mergeThreshold;
    const minLengthUnits = isGeographic ? minLength / 100000 : minLength;

    const kept = paths.filter((path, i) => {
      // Always keep if long enough
      if (lengths[i] >= minLengthUnits || lengths[i] >= totalLength * minRatio) {
        return true;
      }
      // Keep if connected to another path
      for (let j = 0; j < paths.length; j++) {
        if (j !== i && endpointGap(path, paths[j]) < thresholdUnits) {
          return true;
        }
      }
      return false;
    });

    if (kept.length === 0) {
      return geometry; // Don't drop everything
    }

    const normalizedPaths = kept.map((path) => path.map((point) => Array.from(point)));
    const geometryJson = typeof geometry.toJSON === "function"
      ? geometry.toJSON()
      : {
          spatialReference: geometry.spatialReference,
          hasM: geometry.hasM,
          hasZ: geometry.hasZ,
        };

    geometryJson.type = "polyline";
    geometryJson.paths = normalizedPaths;
    return new Polyline(geometryJson);
  }

  async function syncSelectedGraphics() {
    const objectIds = Array.from(state.selectedSegmentIds);

    selectedSegmentsLayer.removeAll();

    if (!objectIds.length) {
      return;
    }

    const features = await querySegments(objectIds);

    features.forEach((feature) => {
      const sourceGeometry =
        typeof feature.geometry?.toJSON === "function" ? feature.geometry.toJSON() : feature.geometry;
      const highlightedFeature = typeof feature.clone === "function" ? feature.clone() : feature;
      highlightedFeature.geometry = filterArtifactPaths(sourceGeometry);
      highlightedFeature.symbol = {
        type: "simple-line",
        color: [11, 107, 122, 255],
        width: 7,
        cap: "round",
        join: "round",
      };
      selectedSegmentsLayer.add(highlightedFeature);
    });
  }

  async function zoomToSegments(objectIds) {
    const features = await querySegments(objectIds);

    if (!features.length) {
      return;
    }

    const isSingleSegment = features.length === 1;
    await view.goTo(buildZoomExtent(features) ?? features, {
      padding: {
        top: 140,
        right: 120,
        bottom: 140,
        left: 120,
      },
      maxScale: 140000,
    });

    await view.goTo(
      {
        center: view.center,
        scale: view.scale * (isSingleSegment ? 1.2 : 1.1),
      },
      {
        animate: false,
      },
    );

    if (isSingleSegment) {
      const popup = buildSegmentPopup(features[0]);
      view.openPopup({
        ...popup,
        location: features[0].geometry.extent?.center ?? view.center,
      });
    }
  }

  window.__waitForSegments = function () {
    return view.when().then(() => {
      return new Promise((resolve) => {
        const check = () => {
          if (state.segments && state.segments.length > 0 && !view.updating) {
            resolve(state.segments.length);
          } else {
            setTimeout(check, 500);
          }
        };
        check();
      });
    });
  };

  window.__selectAndZoomSegment = async function (segmentName) {
    const match = state.segments.find((segment) => segment.label === segmentName);
    if (!match) {
      return false;
    }
    state.selectedSegmentIds.clear();
    state.selectedSegmentIds.add(match.objectId);
    render();
    await syncSelectedGraphics();
    await zoomToSegments([match.objectId]);
    return true;
  };

  function dedupeRoadResults(features) {
    const items = [];
    const seen = new Set();

    features.forEach((feature) => {
      const attributes = feature.attributes ?? {};
      const primaryName = attributes.MAP_LBL || attributes.RTE_NM;

      if (!primaryName) {
        return;
      }

      const key = [
        primaryName,
        attributes.RDBD_TYPE || "",
        attributes.DES_DRCT || "",
      ].join("|");

      if (seen.has(key)) {
        return;
      }

      seen.add(key);
      items.push({
        name: primaryName,
        routeName: attributes.RTE_NM || primaryName,
        routePrefix: attributes.RTE_PRFX || "",
        routeNumber: attributes.RTE_NBR || "",
        roadbedType: attributes.RDBD_TYPE || "Roadway",
        direction: attributes.DES_DRCT || "",
        county: attributes.COUNTY || "",
        beginDfo: attributes.BEGIN_DFO,
        endDfo: attributes.END_DFO,
      });
    });

    return items;
  }

  function formatDfo(value) {
    if (typeof value !== "number" || Number.isNaN(value)) {
      return "N/A";
    }

    return value.toFixed(3);
  }

  async function showRoadPopup(mapPoint) {
    const query = roadsQueryLayer.createQuery();
    query.geometry = mapPoint;
    query.distance = Math.max(10, Math.min(view.resolution * 12, 90));
    query.units = "meters";
    query.spatialRelationship = "intersects";
    query.returnGeometry = false;
    query.outFields = [
      "OBJECTID",
      "RTE_NM",
      "RTE_PRFX",
      "RTE_NBR",
      "MAP_LBL",
      "RDBD_TYPE",
      "DES_DRCT",
      "COUNTY",
      "BEGIN_DFO",
      "END_DFO",
    ];
    query.orderByFields = ["RTE_NM ASC"];
    query.num = 12;

    const result = await roadsQueryLayer.queryFeatures(query);
    const roads = dedupeRoadResults(result.features);

    if (!roads.length) {
      view.closePopup();
      return;
    }

    const title = roads.length === 1 ? roads[0].name : "Nearby TxDOT roadways";
    const content =
      roads.length === 1
        ? `
            <div class="road-popup">
              <p><strong>${escapeHtml(roads[0].routeName)}</strong></p>
              <ul>
                <li>Roadbed Type: ${escapeHtml(roads[0].roadbedType)}</li>
                <li>Name/Number: ${escapeHtml(roads[0].name)}</li>
                <li>Direction: ${escapeHtml(roads[0].direction || "N/A")}</li>
                <li>Begin DFO: ${escapeHtml(formatDfo(roads[0].beginDfo))}</li>
                <li>End DFO: ${escapeHtml(formatDfo(roads[0].endDfo))}</li>
              </ul>
              ${
                roads[0].routePrefix && roads[0].routeNumber
                  ? `<p><a href="${HIGHWAY_DESIGNATION_BASE_URL}?rtePrefix=${encodeURIComponent(
                      roads[0].routePrefix,
                    )}&rteNumber=${encodeURIComponent(
                      roads[0].routeNumber,
                    )}" target="_blank" rel="noreferrer">Open highway designation lookup</a></p>`
                  : ""
              }
            </div>
          `
        : `
            <div class="road-popup">
              <p>Route names near this click</p>
              <ul>
                ${roads
                  .map((road) => {
                    const detailParts = [road.routeName, road.roadbedType, road.direction]
                      .filter(Boolean);
                    return `<li><strong>${escapeHtml(road.name)}</strong>${
                      detailParts.length ? ` · ${escapeHtml(detailParts.join(" · "))}` : ""
                    }</li>`;
                  })
                  .join("")}
              </ul>
            </div>
          `;

    view.openPopup({
      title: escapeHtml(title),
      content,
      location: mapPoint,
    });
  }

  async function loadSegments() {
    const query = segmentsLayer.createQuery();
    query.where = "1=1";
    query.returnGeometry = false;
    query.outFields = [
      "OBJECTID",
      "Readable_SegID",
      "Segment_ID",
      "Highway",
      "County",
      "HSYS",
      "Segment_Length_Mi",
    ];
    query.orderByFields = ["Readable_SegID ASC"];

    const result = await segmentsLayer.queryFeatures(query);

    state.segments = result.features
      .map((feature) => ({
        objectId: feature.attributes.OBJECTID,
        label: feature.attributes.Readable_SegID || feature.attributes.Segment_ID || "Unnamed segment",
        segmentId: feature.attributes.Segment_ID || "",
        highway: feature.attributes.Highway || "",
        county: feature.attributes.County || "",
        hsys: feature.attributes.HSYS || "Unknown",
        lengthMiles: feature.attributes.Segment_Length_Mi,
      }))
      .sort(sortByLabel);

    render();
  }

  elements.searchInput.addEventListener("input", () => {
    state.searchTerm = elements.searchInput.value;
    renderSegments();
  });

  elements.clearSelectionButton.addEventListener("click", async () => {
    state.selectedSegmentIds.clear();
    render();
    await syncSelectedGraphics();
    view.closePopup();
  });

  elements.zoomSelectedButton.addEventListener("click", async () => {
    await zoomToSegments(Array.from(state.selectedSegmentIds));
  });

  view.on("click", async (event) => {
    const hitResponse = await view.hitTest(event, {
      include: [segmentsLayer],
    });

    const segmentHit = hitResponse.results.find((result) => result.graphic?.layer === segmentsLayer);

    if (segmentHit) {
      const objectId = segmentHit.graphic.attributes.OBJECTID;
      state.selectedSegmentIds.add(objectId);
      render();
      await syncSelectedGraphics();
      await zoomToSegments([objectId]);
      return;
    }

    await showRoadPopup(event.mapPoint);
  });

  Promise.all([segmentsLayer.load(), roadsQueryLayer.load(), view.when()])
    .then(loadSegments)
    .catch((error) => {
      console.error(error);
      elements.segmentList.innerHTML =
        '<div class="empty-state">The app could not load the ArcGIS services. Check the browser console for details.</div>';
    });
});
