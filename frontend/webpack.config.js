var path = require('path');
var proxyURL = 'http://localhost:8686';
if (process.env.NODE_ENV == 'docker') {
  proxyURL = 'http://web:8686';
}
module.exports = {
	entry: './src',
	output: {
		path: path.join(__dirname, 'build'),
		filename: 'bundle.js'
	},
	module: {
		rules: [
      {
        test: /\.css$/,
        loader: 'style-loader'
      },
      {
        test: /\.css$/,
        loader: 'css-loader',
        query: {
          modules: true,
          localIdentName: '[name]'
        }
      },
			{
				test: /\.jsx?/i,
				loader: 'babel-loader',
				options: {
					presets: [
						'es2015'
					],
					plugins: [
						['transform-react-jsx']
					]
				}
			}
		]
	},
	devtool: 'source-map',
	devServer: {
    host: '0.0.0.0',
    disableHostCheck: true,
		contentBase: path.join(__dirname, 'src'),
		compress: true,
		historyApiFallback: true,
    proxy: {
      '/api': {
        target: proxyURL,
        secure: false
      }
    }
	},
  resolve: {
    alias: {
      config: path.join(__dirname, 'src', 'config', process.env.NODE_ENV || 'development')
    }
  }
};