import { RouterProvider } from 'react-router-dom';
import { Providers }      from './providers.jsx';
import { router }         from './router.jsx';

/**
 * App — minimal entry: sets up providers and router.
 * No business logic lives here.
 */
export default function App() {
  return (
    <Providers>
      <RouterProvider router={router} />
    </Providers>
  );
}
