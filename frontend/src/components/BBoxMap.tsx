// @ts-nocheck
import React, { useEffect, useRef } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';

interface BBoxMapProps {
  onBBoxChange: (minLon: number, minLat: number, maxLon: number, maxLat: number) => void;
  onClear: () => void; // Added so the parent knows to erase the inputs
}

export default function BBoxMap({ onBBoxChange, onClear }: BBoxMapProps) {
  const mapRef = useRef(null);
  const currentBoxRef = useRef(null); // Keep track of the box so the clear button can delete it
  const mapInstanceRef = useRef(null);

  useEffect(() => {
    if (!mapRef.current) return;

    // Initialize map 
    const map = L.map(mapRef.current, { boxZoom: false }).setView([39.0, -98.0], 4);
    mapInstanceRef.current = map;

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap'
    }).addTo(map);

    let startPoint = null;

    map.on('mousedown', (e) => {
      if (!e.originalEvent.shiftKey) return; 
      
      map.dragging.disable();
      startPoint = e.latlng;

      if (currentBoxRef.current) {
        map.removeLayer(currentBoxRef.current);
      }

      currentBoxRef.current = L.rectangle([startPoint, startPoint], {
        color: '#3b82f6',
        weight: 2,
        fillOpacity: 0.2
      }).addTo(map);
    });

    map.on('mousemove', (e) => {
      if (!startPoint || !currentBoxRef.current) return;
      currentBoxRef.current.setBounds([startPoint, e.latlng]);
    });

    map.on('mouseup', (e) => {
      if (!startPoint) return; 

      map.dragging.enable(); 
      startPoint = null;

      if (currentBoxRef.current) {
        const bounds = currentBoxRef.current.getBounds();
        onBBoxChange(
          bounds.getWest(),
          bounds.getSouth(),
          bounds.getEast(),
          bounds.getNorth()
        );
      }
    });

    return () => {
      map.remove();
    };
  }, []);

  // Erases the box from the map and tells the parent to clear the text inputs
  const handleClear = (e) => {
    e.preventDefault(); // Prevents form submission
    if (currentBoxRef.current && mapInstanceRef.current) {
      mapInstanceRef.current.removeLayer(currentBoxRef.current);
      currentBoxRef.current = null;
    }
    onClear();
  };

  return (
    <div style={{ position: 'relative', userSelect: 'none', WebkitUserSelect: 'none' }}>
      
      {/* Instruction banner */}
      {/* Instruction banner */}
      <div style={{
        position: 'absolute', top: 10, right: 10, zIndex: 1000, 
        backgroundColor: 'var(--surface)',
        color: 'var(--text)',
        border: '1px solid var(--border)',
        padding: '4px 8px', 
        borderRadius: '4px', fontSize: '0.85rem', fontWeight: 'bold', pointerEvents: 'none'
      }}>
        Hold SHIFT and Drag to draw box
      </div>

      {/* Clear Button - styled to match Leaflet controls and positioned below zoom */}
      {/* Clear Button */}
      <button 
        type="button"
        onClick={handleClear}
        title="Clear bounding box"
        style={{
          position: 'absolute', top: '80px', left: '10px', zIndex: 1000,
          backgroundColor: 'var(--surface)',
          color: 'var(--text)',
          border: '2px solid var(--border)',
          borderRadius: '4px', width: '34px', height: '34px',
          cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: '16px', padding: 0, outline: 'none'
        }}
      >
        🗑️
      </button>

      <div 
        ref={mapRef} 
        style={{ height: '300px', width: '100%', zIndex: 0, borderRadius: '8px', border: '1px solid #ccc' }} 
      />
    </div>
  );
}