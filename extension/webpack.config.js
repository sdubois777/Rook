const CopyPlugin = require('copy-webpack-plugin')
const webpack = require('webpack')
const path = require('path')

// The STORE build (`--mode production`) ships NO source maps and hides the debug
// capture feature; the DEV build (`--mode development`) keeps both for Stephen.
module.exports = (env, argv) => {
  const isProd = argv.mode === 'production'
  return {
    entry: {
      background: './src/background/service_worker.js',
      yahoo_draft: './src/content_scripts/yahoo_draft.js',
      yahoo_snake_draft: './src/content_scripts/yahoo_snake_draft.js',
      yahoo_draft_main: './src/content_scripts/yahoo_draft_main.js',
      yahoo_snake_draft_main: './src/content_scripts/yahoo_snake_draft_main.js',
      yahoo_auth: './src/content_scripts/yahoo_auth.js',
      espn_draft: './src/content_scripts/espn_draft.js',
      espn_auth: './src/content_scripts/espn_auth.js',
      sleeper_draft: './src/content_scripts/sleeper_draft.js',
      sleeper_draft_main: './src/content_scripts/sleeper_draft_main.js',
      popup: './src/popup/popup.js',
    },
    output: {
      path: path.resolve(__dirname, 'dist'),
      filename: '[name].js',
      clean: true,
    },
    // No source maps in the store build (they'd otherwise ship in the zip).
    devtool: isProd ? false : 'cheap-source-map',
    module: {
      rules: [{
        resourceQuery: /raw/,
        type: 'asset/source',
      }],
    },
    plugins: [
      new CopyPlugin({
        patterns: [
          { from: 'manifest.json', to: '.' },
          { from: 'src/popup/popup.html', to: 'popup/' },
          { from: 'src/popup/popup.css', to: 'popup/' },
          { from: 'icons', to: 'icons' },
        ],
      }),
      // __DEV__ is true only in the dev build; the store build tree-shakes the
      // debug capture UI out of the popup.
      new webpack.DefinePlugin({ __DEV__: JSON.stringify(!isProd) }),
    ],
  }
}
