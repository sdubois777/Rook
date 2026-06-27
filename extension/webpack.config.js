const CopyPlugin = require('copy-webpack-plugin')
const path = require('path')
module.exports = {
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
  devtool: 'cheap-source-map',
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
  ],
}