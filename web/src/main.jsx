import React from "react";
import ReactDOM from "react-dom"; // React 17 API (no createRoot)
import App from "./App.jsx";
import "./App.css";

ReactDOM.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
  document.getElementById("root")
);
