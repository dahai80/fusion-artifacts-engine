// Issue #63: ErrorBoundary backing the 4-level fallback chain.
// A primitive that throws during render is caught here so the dispatcher
// can move to the next fallback level — mirrors visual_error_boundary.py
// (never propagates exceptions to the caller).

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** Rendered when a child throws. Receives the caught error. */
  fallback: (error: Error) => ReactNode;
}

interface State {
  error: Error | null;
}

export class VisualErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Same contract as VisualErrorBoundary.py: log metadata, never rethrow.
    console.error(
      "[artifacts-visual] primitive render failed:",
      error.message,
      info.componentStack,
    );
  }

  render(): ReactNode {
    if (this.state.error) {
      return this.props.fallback(this.state.error);
    }
    return this.props.children;
  }
}
