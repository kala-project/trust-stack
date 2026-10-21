import { Component, type ReactNode } from "react";

export class PageBoundary extends Component<{children: ReactNode}, {failed: boolean}> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (!this.state.failed) return this.props.children;
    return <section role="alert"><h1>This page is temporarily unavailable</h1><p>Your research records have not changed. Reload this page or open another research item from the navigation.</p><button onClick={() => this.setState({failed:false})}>Reload this page</button></section>;
  }
}
