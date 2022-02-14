from blik._plugin.widget import volume_selector
import napari


def test_volume_widget(make_napari_viewer):
    viewer = make_napari_viewer()

    wdg = volume_selector()
    viewer.window.add_dock_widget(wdg)

    with napari.layers._source.layer_source(reader_plugin='blik'):
        viewer.add_points(metadata={'volume': 'test'})
    assert wdg.volume.choices == ('test',)
