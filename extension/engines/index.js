import * as bing from "./bing.js";

const ENGINES = { bing };

export const DEFAULT_ENGINE = "bing";

export function getEngine(name) {
  const engine = ENGINES[name || DEFAULT_ENGINE];
  if (!engine) {
    throw new Error(`unknown engine: ${name}`);
  }
  return engine;
}
