/// <reference lib="webworker" />
/** Web worker do layout `dagre` para desenhos grandes. */
import { type DagreInput, dagrePositions } from "./dagre-layout";

self.onmessage = (event: MessageEvent<{ id: number; input: DagreInput }>) => {
  const { id, input } = event.data;
  const started = performance.now();
  const positions = dagrePositions(input);
  self.postMessage({ id, positions, ms: Math.round(performance.now() - started) });
};
