"use client";

// AG Grid (Community) set in the Provenant tokens, so it follows light and dark mode.
import { AllCommunityModule, ModuleRegistry, themeQuartz, type ColDef } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";

ModuleRegistry.registerModules([AllCommunityModule]);

export const gridTheme = themeQuartz.withParams({
  backgroundColor: "var(--bg)",
  foregroundColor: "var(--text)",
  accentColor: "var(--accent)",
  borderColor: "var(--line)",
  chromeBackgroundColor: "var(--surface)",
  headerBackgroundColor: "var(--surface)",
  headerTextColor: "var(--muted)",
  rowHoverColor: "var(--surface)",
  fontFamily: "var(--font-sans)",
  fontSize: 13,
  headerFontWeight: 500,
  spacing: 7,
});

export function Grid<T>({ rows, columns, height = 420 }: { rows: T[]; columns: ColDef<T>[]; height?: number }) {
  return (
    // Short tables size to their rows; long ones scroll inside a fixed height.
    <div style={rows.length <= 10 ? undefined : { height }} className="overflow-hidden rounded-xl border border-line">
      <AgGridReact<T>
        domLayout={rows.length <= 10 ? "autoHeight" : "normal"}
        theme={gridTheme}
        rowData={rows}
        columnDefs={columns}
        defaultColDef={{ sortable: true, filter: true, resizable: true, minWidth: 90, flex: 1 }}
        animateRows
        suppressCellFocus
      />
    </div>
  );
}
