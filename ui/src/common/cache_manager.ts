// Copyright (C) 2021 The Android Open Source Project
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

/**
 * This file deals with caching traces in the browser's Cache storage. The
 * traces are cached so that the UI can gracefully reload a trace when the tab
 * containing it is discarded by Chrome (e.g. because the tab was not used for
 * a long time) or when the user accidentally hits reload.
 */
import {getErrorMessage} from './errors';
import {TraceArrayBufferSource, TraceSource} from './state';

const TRACE_CACHE_NAME = 'cached_traces';
const TRACE_CACHE_SIZE = 10;

let LAZY_CACHE: Cache|undefined = undefined;

// Occasionally operations using the cache API throw:
// 'UnknownError: Unexpected internal error. {}'
// It's not clear under which circumstances this can occur. A dive of
// the Chromium code didn't shed much light:
// https://source.chromium.org/chromium/chromium/src/+/main:third_party/blink/renderer/modules/cache_storage/cache_storage_error.cc;l=26;drc=4cfe86482b000e848009077783ba35f83f3c3cfe
// https://source.chromium.org/chromium/chromium/src/+/main:content/browser/cache_storage/cache_storage_cache.cc;l=1686;drc=ab68c05beb790d04d1cb7fd8faa0a197fb40d399
// Given the error is not actionable at present and caching is 'best
// effort' in any case ignore this error. We will want to throw for
// errors in general though so as not to hide errors we actually could
// fix.
// See b/227785665 for an example.
function ignoreNonActionableErrors<T>(e: unknown, result: T): T {
  if (getErrorMessage(e).includes('UnknownError')) {
    return result;
  } else {
    throw e;
  }
}

async function getCache(): Promise<Cache> {
  if (LAZY_CACHE !== undefined) {
    return LAZY_CACHE;
  }
  LAZY_CACHE = await caches.open(TRACE_CACHE_NAME);
  return LAZY_CACHE;
}

async function cacheDelete(key: Request): Promise<boolean> {
  try {
    const cache = await getCache();
    return cache.delete(key);
  } catch (e) {
    return ignoreNonActionableErrors(e, false);
  }
}

async function cachePut(key: string, value: Response): Promise<void> {
  try {
    const cache = await getCache();
    cache.put(key, value);
  } catch (e) {
    ignoreNonActionableErrors(e, undefined);
  }
}

async function cacheMatch(key: Request|string): Promise<Response|undefined> {
  try {
    const cache = await getCache();
    return cache.match(key);
  } catch (e) {
    return ignoreNonActionableErrors(e, undefined);
  }
}

async function cacheKeys(): Promise<readonly Request[]> {
  try {
    const cache = await getCache();
    return cache.keys();
  } catch (e) {
    return ignoreNonActionableErrors(e, []);
  }
}

export async function cacheTrace(
    traceSource: TraceSource, traceUuid: string): Promise<boolean> {
  let trace;
  let title = '';
  let fileName = '';
  let url = '';
  let contentLength = 0;
  let localOnly = false;
  switch (traceSource.type) {
    case 'ARRAY_BUFFER':
      trace = traceSource.buffer;
      title = traceSource.title;
      fileName = traceSource.fileName || '';
      url = traceSource.url || '';
      contentLength = traceSource.buffer.byteLength;
      localOnly = traceSource.localOnly || false;
      break;
    case 'FILE':
      trace = await traceSource.file.arrayBuffer();
      title = traceSource.file.name;
      contentLength = traceSource.file.size;
      break;
    default:
      return false;
  }

  const headers = new Headers([
    ['x-trace-title', title],
    ['x-trace-url', url],
    ['x-trace-filename', fileName],
    ['x-trace-local-only', `${localOnly}`],
    ['content-type', 'application/octet-stream'],
    ['content-length', `${contentLength}`],
    [
      'expires',
      // Expires in a week from now (now = upload time)
      (new Date((new Date()).getTime() + (1000 * 60 * 60 * 24 * 7)))
          .toUTCString(),
    ],
  ]);
  await deleteStaleEntries();
  await cachePut(
      `/_${TRACE_CACHE_NAME}/${traceUuid}`, new Response(trace, {headers}));
  return true;
}

export async function tryGetTrace(traceUuid: string):
    Promise<TraceArrayBufferSource|undefined> {
  await deleteStaleEntries();
  const response = await cacheMatch(`/_${TRACE_CACHE_NAME}/${traceUuid}`);

  if (!response) return undefined;
  return {
    type: 'ARRAY_BUFFER',
    buffer: await response.arrayBuffer(),
    title: response.headers.get('x-trace-title') || '',
    fileName: response.headers.get('x-trace-filename') || undefined,
    url: response.headers.get('x-trace-url') || undefined,
    uuid: traceUuid,
    localOnly: response.headers.get('x-trace-local-only') === 'true',
  };
}

async function deleteStaleEntries() {
  // Loop through stored traces and invalidate all but the most recent
  // TRACE_CACHE_SIZE.
  const keys = await cacheKeys();
  const storedTraces: Array<{key: Request, date: Date}> = [];
  const now = new Date();
  const deletions = [];
  for (const key of keys) {
    const existingTrace = await cacheMatch(key);
    if (existingTrace === undefined) {
      continue;
    }
    const expires = existingTrace.headers.get('expires');
    if (expires === undefined || expires === null) {
      // Missing `expires`, so give up and delete which is better than
      // keeping it around forever.
      deletions.push(cacheDelete(key));
      continue;
    }
    const expiryDate = new Date(expires);
    if (expiryDate < now) {
      deletions.push(cacheDelete(key));
    } else {
      storedTraces.push({key, date: expiryDate});
    }
  }

  // Sort the traces descending by time, such that most recent ones are placed
  // at the beginning. Then, take traces from TRACE_CACHE_SIZE onwards and
  // delete them from cache.
  const oldTraces =
      storedTraces.sort((a, b) => b.date.getTime() - a.date.getTime())
          .slice(TRACE_CACHE_SIZE);
  for (const oldTrace of oldTraces) {
    deletions.push(cacheDelete(oldTrace.key));
  }

  // TODO(hjd): Wrong Promise.all here, should use the one that
  // ignores failures but need to upgrade TypeScript for that.
  await Promise.all(deletions);
}
