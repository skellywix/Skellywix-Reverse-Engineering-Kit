import { Navigate, Route, Routes } from 'react-router-dom';
import Shell from './Shell';
import Convert from './pages/Convert';
import Inspect from './pages/Inspect';
import Reconstruct from './pages/Reconstruct';
import Releases from './pages/Releases';
import Runtime from './pages/Runtime';

export default function App(): React.JSX.Element {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route path="/reconstruct" element={<Reconstruct />} />
        <Route path="/reconstruct/runs/:id" element={<Reconstruct />} />
        <Route path="/inspect" element={<Inspect />} />
        <Route path="/convert" element={<Convert />} />
        <Route path="/convert/projects/:id" element={<Convert />} />
        <Route path="/runtime" element={<Runtime />} />
        <Route path="/releases" element={<Releases />} />
        <Route path="/" element={<Navigate replace to="/reconstruct" />} />
        <Route path="*" element={<Navigate replace to="/reconstruct" />} />
      </Route>
    </Routes>
  );
}
