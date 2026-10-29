import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, MemoryRouter } from "react-router-dom";
import App from "./App";
import { STATIC } from "./api";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {STATIC ? <MemoryRouter initialEntries={["/opportunities"]}><App /></MemoryRouter> : <BrowserRouter><App /></BrowserRouter>}
  </React.StrictMode>,
);
