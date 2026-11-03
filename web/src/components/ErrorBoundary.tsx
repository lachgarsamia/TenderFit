import { Component, type ErrorInfo, type ReactNode } from "react";

/** One bad response must never blank the application: the error stays on the page it came from. */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Screen failed to render", error, info.componentStack);
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="empty" role="alert">
        <strong>This page could not be shown.</strong>
        The rest of TenderFit still works. Try again, or open another page from the menu.
        <div style={{ marginTop: 12 }}><button className="btn" onClick={() => this.setState({ error: null })}>Try again</button></div>
      </div>
    );
  }
}
