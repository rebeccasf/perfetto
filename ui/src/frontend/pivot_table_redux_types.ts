// Copyright (C) 2022 The Android Open Source Project
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import {ColumnType} from '../common/query_result';

// Node in the hierarchical pivot tree. Only leaf nodes contain data from the
// query result.
export interface PivotTree {
  // Whether the node should be collapsed in the UI, false by default and can
  // be toggled with the button.
  isCollapsed: boolean;

  // Non-empty only in internal nodes.
  children: Map<ColumnType, PivotTree>;
  aggregates: ColumnType[];

  // Non-empty only in leaf nodes.
  rows: ColumnType[][];
}

export type AggregationFunction = 'COUNT'|'SUM'|'MIN'|'MAX';

// Queried "table column" is either:
// 1. A real one, represented as object with table and column name.
// 2. Pseudo-column 'count' that's rendered as '1' in SQL to use in queries like
// `select sum(1), name from slice group by name`.

export interface RegularColumn {
  kind: 'regular';
  table: string;
  column: string;
}

export interface ArgumentColumn {
  kind: 'argument';
  argument: string;
}

export type TableColumn = RegularColumn|ArgumentColumn;

export function tableColumnEquals(t1: TableColumn, t2: TableColumn): boolean {
  switch (t1.kind) {
    case 'argument': {
      return t2.kind === 'argument' && t1.argument === t2.argument;
    }
    case 'regular': {
      return t2.kind === 'regular' && t1.table === t2.table &&
          t1.column === t2.column;
    }
  }
}

export function toggleEnabled(
    arr: TableColumn[], column: TableColumn, enabled: boolean): void {
  if (enabled &&
      arr.find((value) => tableColumnEquals(column, value)) === undefined) {
    arr.push(column);
  }
  if (!enabled) {
    const index = arr.findIndex((value) => tableColumnEquals(column, value));
    if (index !== -1) {
      arr.splice(index, 1);
    }
  }
}

export interface Aggregation {
  aggregationFunction: AggregationFunction;
  column: TableColumn;
}

// Used to convert TableColumn to a string in order to store it in a Map, as
// ES6 does not support compound Set/Map keys. This function should only be used
// for interning keys, and does not have any requirements beyond different
// TableColumn objects mapping to different strings.
export function columnKey(tableColumn: TableColumn): string {
  switch (tableColumn.kind) {
    case 'argument': {
      return `argument:${tableColumn.argument}`;
    }
    case 'regular': {
      return `${tableColumn.table}.${tableColumn.column}`;
    }
    default: {
      throw new Error(`malformed table column ${tableColumn}`);
    }
  }
}

export function aggregationKey(aggregation: Aggregation): string {
  return `${aggregation.aggregationFunction}:${columnKey(aggregation.column)}`;
}
