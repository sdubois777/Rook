import { Component } from 'react'

/**
 * Catches render-time errors anywhere below it and shows a recovery
 * screen instead of blanking the whole app. React error boundaries
 * must be class components — there is no hook equivalent.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('Unhandled render error:', error, info.componentStack)
  }

  handleReload = () => {
    this.setState({ error: null })
    window.location.reload()
  }

  render() {
    if (this.state.error) {
      // A compact, scoped fallback may be supplied for wrapping a single
      // panel; otherwise fall back to the full-screen recovery screen.
      if (this.props.fallback !== undefined) {
        return this.props.fallback
      }
      return (
        <div className="min-h-screen bg-gray-950 flex items-center justify-center p-6">
          <div className="max-w-md text-center">
            <h1 className="text-2xl font-bold text-white mb-2">
              Something went wrong
            </h1>
            <p className="text-gray-400 mb-6">
              An unexpected error occurred while rendering this page.
            </p>
            <button
              onClick={this.handleReload}
              className="bg-blue-600 hover:bg-blue-500 text-white font-medium px-6 py-3 rounded-lg transition-colors"
            >
              Reload
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
