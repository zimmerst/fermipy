# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
Merge source maps to build composite sources
"""
from __future__ import absolute_import, division, print_function

import os
import sys

import argparse
import yaml

from astropy.io import fits
from fermipy.skymap import HpxMap

from fermipy.utils import load_yaml

from fermipy.jobs.scatter_gather import ConfigMaker, build_sg_from_link
from fermipy.jobs.lsf_impl import make_nfs_path, get_lsf_default_args, LSF_Interface
from fermipy.jobs.chain import add_argument, Link
from fermipy.jobs.file_archive import FileFlags
from fermipy.diffuse.binning import Component
from fermipy.diffuse.name_policy import NameFactory
from fermipy.diffuse import defaults as diffuse_defaults
from fermipy.diffuse.model_manager import make_library

NAME_FACTORY = NameFactory()


class GtInitModel(Link):
    """Small class to preprate files fermipy analysis.

    Specifically this create the srcmap_manifest and fermipy_config_yaml files
    """
    default_options = dict(comp=diffuse_defaults.diffuse['comp'],
                           data=diffuse_defaults.diffuse['data'],
                           library=diffuse_defaults.diffuse['library'],
                           models=diffuse_defaults.diffuse['models'],
                           hpx_order=diffuse_defaults.diffuse['hpx_order_fitting'])
    
    def __init__(self, **kwargs):
        """C'tor
        """
        parser = argparse.ArgumentParser(usage = "fermipy-init-model [options]",
                                         description = "Initialize model fitting directory")
        Link.__init__(self, kwargs.pop('linkname', 'init-model'),
                      appname='fermipy-init-model',
                      parser=parser,
                      options=GtInitModel.default_options.copy(),
                      **kwargs)

    def run_analysis(self, argv):
        """ Build the manifest for all the models
        """
        args = self._parser.parse_args(argv)
        components = Component.build_from_yamlfile(args.comp)
        NAME_FACTORY.update_base_dict(args.data)
        model_dict = make_library(**args.__dict__)
        model_manager = model_dict['ModelManager']
        models = load_yaml(args.models)
        data = args.data
        hpx_order = args.hpx_order
        for modelkey, modelpath in models.items():
            model_manager.make_srcmap_manifest(modelkey, components, data)
            fermipy_config = model_manager.make_fermipy_config_yaml(modelkey, components, data, 
                                                                    hpx_order=hpx_order, 
                                                                    irf_ver=NAME_FACTORY.irf_ver())
            


class GtAssembleModel(Link):
    """Small class to assemple source map files for fermipy analysis.

    This is useful for re-merging after parallelizing source map creation.
    """
    default_options = dict(input=(None, 'Input yaml file', str),
                           compname=(None, 'Component name.', str),
                           hpx_order=diffuse_defaults.diffuse['hpx_order_fitting'])

    def __init__(self, **kwargs):
        """C'tor
        """
        parser = argparse.ArgumentParser(usage="fermipy-assemble-model [options]", 
                                         description="Copy source maps from the library to a analysis directory")
        Link.__init__(self, kwargs.pop('linkname', 'assemble-model'),
                      appname='fermipy-assemble-model',
                      parser=parser,
                      options=GtAssembleModel.default_options.copy(),
                      file_args=dict(input=FileFlags.input_mask),
                      **kwargs)

 
    @staticmethod
    def copy_ccube(ccube, outsrcmap, hpx_order):
        """Copy a counts cube into outsrcmap file
        reducing the HEALPix order to hpx_order if needed.
        """
        sys.stdout.write ("  Copying counts cube from %s to %s\n" % (ccube, outsrcmap))
        try:
            hdulist_in = fits.open(ccube)
        except IOError:
            hdulist_in = fits.open("%s.gz" % ccube)

        hpx_order_in = hdulist_in[1].header['ORDER']

        if hpx_order_in > hpx_order:
            hpxmap = HpxMap.create_from_hdulist(hdulist_in)
            hpxmap_out = hpxmap.ud_grade(hpx_order, preserve_counts=True)
            hpxlist_out = hdulist_in
            #hpxlist_out['SKYMAP'] = hpxmap_out.create_image_hdu()
            hpxlist_out[1] = hpxmap_out.create_image_hdu()
            hpxlist_out[1].name = 'SKYMAP'
            hpxlist_out.writeto(outsrcmap)
            return hpx_order
        else:
            os.system('cp %s %s' % (ccube, outsrcmap))
            #os.system('cp %s.gz %s.gz' % (ccube, outsrcmap))
            #os.system('gunzip -f %s.gz' % (outsrcmap))
        return None

    @staticmethod
    def open_outsrcmap(outsrcmap):
        """Open and return the outsrcmap file in append mode """
        outhdulist = fits.open(outsrcmap, 'append')
        return outhdulist

    @staticmethod
    def append_hdus(hdulist, srcmap_file, source_names, hpx_order):
        """Append HEALPix maps to a list

        Parameters
        ----------

        hdulist : list
            The list being appended to
        srcmap_file : str
            Path to the file containing the HDUs
        source_names : list of str
            Names of the sources to extract from srcmap_file
        hpx_order : int
            Maximum order for maps
        """
        sys.stdout.write("  Extracting %i sources from %s" % (len(source_names), srcmap_file))
        try:
            hdulist_in = fits.open(srcmap_file)
        except IOError:
            try:
                hdulist_in = fits.open('%s.gz' % srcmap_file)
            except IOError:
                 sys.stdout.write("  Missing file %s\n" % srcmap_file)
                 return

        for source_name in source_names:
            sys.stdout.write('.')
            sys.stdout.flush()
            if hpx_order is None:
                hdulist.append(hdulist_in[source_name])
            else:
                try:
                    hpxmap = HpxMap.create_from_hdulist(hdulist_in, hdu=source_name)
                except IndexError:
                    print("  Index error on source %s in file %s" % (source_name, srcmap_file))
                    continue
                except KeyError:
                    print("  Key error on source %s in file %s" % (source_name, srcmap_file))
                    continue
                hpxmap_out = hpxmap.ud_grade(hpx_order, preserve_counts=True)
                hdulist.append(hpxmap_out.create_image_hdu(name=source_name))
        sys.stdout.write("\n")
        hdulist.flush()
        hdulist_in.close()

    @staticmethod
    def assemble_component(compname, compinfo, hpx_order):
        """Assemble the source map file for one binning component

        Parameters
        ----------

        compname : str
            The key for this component (e.g., E0_PSF3)
        compinfo : dict
            Information about this component
        hpx_order : int
            Maximum order for maps

        """
        sys.stdout.write ("Working on component %s\n" % compname)
        ccube = compinfo['ccube']
        outsrcmap = compinfo['outsrcmap']
        source_dict = compinfo['source_dict']

        hpx_order = GtAssembleModel.copy_ccube(ccube, outsrcmap, hpx_order)
        hdulist = GtAssembleModel.open_outsrcmap(outsrcmap)

        for comp_name in sorted(source_dict.keys()):
            source_info = source_dict[comp_name]
            source_names = source_info['source_names']
            srcmap_file = source_info['srcmap_file']
            GtAssembleModel.append_hdus(hdulist, srcmap_file,
                                        source_names, hpx_order)
        sys.stdout.write("Done!\n")

    def run_analysis(self, argv):
        """Assemble the source map file for one binning component
        FIXME
        """
        args = self._parser.parse_args(argv)
        manifest = yaml.safe_load(open(args.input))

        compname = args.compname
        value = manifest[compname]
        GtAssembleModel.assemble_component(compname, value, args.hpx_order)


class ConfigMaker_AssembleModel(ConfigMaker):
    """Small class to generate configurations for this script

    Parameters
    ----------

    --compname  : binning component definition yaml file
    --data      : datset definition yaml file
    --models    : model definitino yaml file
    args        : Names of models to assemble source maps for
    """
    default_options = dict(comp=diffuse_defaults.diffuse['comp'],
                           data=diffuse_defaults.diffuse['data'],
                           models=diffuse_defaults.diffuse['models'])

    def __init__(self, link, **kwargs):
        """C'tor
        """
        ConfigMaker.__init__(self, link,
                             options=kwargs.get('options',
                                                ConfigMaker_AssembleModel.default_options.copy()))


    def build_job_configs(self, args):
        """Hook to build job configurations
        """
        job_configs = {}

        components = Component.build_from_yamlfile(args['comp'])
        NAME_FACTORY.update_base_dict(args['data'])

        models = load_yaml(args['models'])
        
        for modelkey, modelpath in models.items():
            manifest = os.path.join('analysis', 'model_%s' % modelkey,
                                    'srcmap_manifest_%s.yaml' % modelkey)
            for comp in components:
                key = comp.make_key('{ebin_name}_{evtype_name}')
                fullkey = "%s_%s"%(modelkey, key)
                outfile = NAME_FACTORY.merged_srcmaps(modelkey=modelkey,
                                                      component=key,
                                                      coordsys=comp.coordsys,
                                                      mktime='none',
                                                      irf_ver=NAME_FACTORY.irf_ver())
                logfile = make_nfs_path(outfile.replace('.fits', '.log'))
                job_configs[fullkey] = dict(input=manifest,
                                            compname=key,
                                            logfile=logfile)
        return job_configs

def create_link_init_model(**kwargs):
    """Build and return a `Link` object that can invoke GtInitModel"""
    gtinit = GtInitModel(**kwargs)
    return gtinit


def create_link_assemble_model(**kwargs):
    """Build and return a `Link` object that can invoke GtAssembleModel"""
    gtassemble = GtAssembleModel(**kwargs)
    return gtassemble


def create_sg_assemble_model(**kwargs):
    """Build and return a ScatterGather object that can invoke this script"""

    gtassemble = GtAssembleModel(**kwargs)
    link = gtassemble

    appname = kwargs.pop('appname', 'fermipy-assemble-model-sg')

    batch_args = get_lsf_default_args()    
    batch_interface = LSF_Interface(**batch_args)

    usage = "%s [options]"%(appname)
    description = "Copy source maps from the library to a analysis directory"

    config_maker = ConfigMaker_AssembleModel(link)
    sg = build_sg_from_link(link, config_maker,
                            interface=batch_interface,
                            usage=usage,
                            description=description,
                            appname=appname,
                            **kwargs)
    return sg


def main_init():
    """Entry point for command line use for init job """
    gtsmp = GtInitModel()
    gtsmp.run_analysis(sys.argv[1:])

def main_single():
    """Entry point for command line use for single job """
    gtsmp = GtAssembleModel()
    gtsmp.run_analysis(sys.argv[1:])


def main_batch():
    """Entry point for command line use  for dispatching batch jobs """
    lsf_sg = create_sg_assemble_model()
    lsf_sg(sys.argv)

if __name__ == '__main__':
    main_single()
