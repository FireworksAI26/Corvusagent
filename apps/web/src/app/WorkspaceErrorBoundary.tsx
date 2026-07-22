import { Component, type ErrorInfo, type ReactNode } from "react";

interface WorkspaceErrorBoundaryProps {
  children: ReactNode;
}

interface WorkspaceErrorBoundaryState {
  failed: boolean;
}

export class WorkspaceErrorBoundary extends Component<
  WorkspaceErrorBoundaryProps,
  WorkspaceErrorBoundaryState
> {
  state: WorkspaceErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): WorkspaceErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("Corvus workspace render failed", error, errorInfo);
  }

  render() {
    if (this.state.failed) {
      return (
        <section className="workspace-error" role="alert">
          <p className="eyebrow">Workspace interrupted</p>
          <h1>Corvus could not open this view.</h1>
          <p>Your saved work is unchanged. Reload the app to try opening the workspace again.</p>
          <button className="button button--primary" onClick={() => window.location.reload()} type="button">
            Reload workspace
          </button>
        </section>
      );
    }

    return this.props.children;
  }
}
