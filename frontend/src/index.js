import React from 'react';
import ReactDOM from 'react-dom';
function init() {
	let App = require('./components/app').default;
  ReactDOM.render(<App />, document.getElementById('app'));
}
init();
if (module.hot) module.hot.accept('./components/app', init);