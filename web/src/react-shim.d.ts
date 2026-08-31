// shim: make React.useEffect / useRef / useState visible to TS
import 'react';
declare module 'react' {
  function useEffect(effect: () => (void | (() => void)), deps?: readonly any[]): void;
  function useRef<T>(initial: T): { current: T };
  function useState<T>(initial: T | (() => T)): [T, (next: T | ((prev: T) => T)) => void];
}
